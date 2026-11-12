"""Unreal Auto QA controller and local Decisions panel."""

import argparse
import hashlib
import html
import json
import math
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import warnings
import webbrowser
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from getpass import GetPassWarning, getpass
from functools import lru_cache
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from qa_credentials import load_key, save_key
from qa_catalog import action_candidates, discovered_actions, read_catalog
from qa_config import read_config
from qa_nodes import compile_node
from qa_mock import MAPPINGS, MOCK_CATALOG, Rejected, make_server, read_json, reply, validate_step
from qa_project import (copy_project_inputs, file_hash, inspect_project, install_plugin, local_path, plugin_preview, project_candidates,
                        project_fingerprint, recover_project)
from qa_scenario import evaluate_assertions, scenario_nodes
from spikes.decisions_smoke import choice_body, classify_answer, request_choice

ROOT = Path(__file__).resolve().parent
FROZEN = getattr(sys, "frozen", False)
OUT = (Path(sys.executable).resolve().parent if FROZEN else ROOT) / "runs"
LIVE_INTERVAL_SECONDS = 2
MAX_SIMULTANEOUS_INPUTS = 12
# shortcut: reserve estimated tokens for the current short text payload, replace with a
# verified upper bound before adding arbitrary-length game observations.
INPUT_TOKEN_RESERVE = 1024


@lru_cache(maxsize=1)
def runtime_fingerprint():
    files = [Path(sys.executable)] if FROZEN else [
        ROOT / name for name in ("qa.py", "qa_mock.py", "qa_nodes.py", "qa_catalog.py",
                                 "qa_config.py", "qa_scenario.py", "spikes/decisions_smoke.py")
    ]
    digest = hashlib.sha256()
    for path in files:
        with path.open("rb") as file:
            digest.update(hashlib.file_digest(file, "sha256").digest() if sys.version_info >= (3, 11)
                          else hashlib.sha256(file.read()).digest())
    return digest.hexdigest()


@contextmanager
def transport(game=None):
    server, token = make_server(game)
    with server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        try:
            yield loopback_client(server.server_port, token)
        finally:
            server.shutdown()
            thread.join()


class NoLoopbackRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def loopback_client(port, token, *, connection_error="MOCK_CONNECTION_ERROR"):
    opener = build_opener(ProxyHandler({}), NoLoopbackRedirect())
    active_token = token

    def call(path, payload=None):
        nonlocal active_token
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(f"http://127.0.0.1:{port}/qa/v1{path}", data=data,
                          headers={"Authorization": f"Bearer {token if path == '/session' else active_token}",
                                   "Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=4) as response:
                value = json.load(response)
            if path == "/session":
                active_token = value.pop("session_token")
            elif path == "/session/stop":
                active_token = token
            return value
        except HTTPError as error:
            with error:
                if 300 <= error.code < 400:
                    raise Rejected("LOOPBACK_REDIRECT") from None
                try:
                    detail = json.load(error)
                except (ValueError, UnicodeError):
                    detail = None
                code = detail.get("error") if isinstance(detail, dict) else None
                raise Rejected(code if isinstance(code, str) else "LOOPBACK_HTTP_ERROR") from None
        except (URLError, TimeoutError, ConnectionError, HTTPException):
            raise Rejected(connection_error) from None

    return call


@contextmanager
def ue_transport(executable, diagnostics=None):
    if os.name != "nt":
        raise Rejected("WIN64_REQUIRED")
    executable = executable.resolve(strict=True)
    if not executable.is_file() or executable.suffix.lower() != ".exe":
        raise Rejected("INVALID_GAME_EXECUTABLE")
    token = secrets.token_hex(32)
    # Pass Windows runtime paths without inheriting credentials from the controller environment.
    env = {key: os.environ[key] for key in (
        "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE",
        "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMSPEC",
    ) if key in os.environ}
    env["UNREALAUTOQA_BRIDGE_BOOTSTRAP"] = token
    user_dir = OUT / f"bridge-{uuid4().hex}"
    user_dir.mkdir(parents=True)
    engine_log = user_dir / "game.log"
    if diagnostics is not None:
        diagnostics["engine_log"] = str(engine_log)
    command = [str(executable), "-RenderOffscreen", "-nosound", "-unattended", "-NoSplash",
               "-NoCrashDialog", f"-UserDir={user_dir}", f"-abslog={engine_log}"]
    with subprocess.Popen(command, cwd=executable.parent, env=env, creationflags=subprocess.CREATE_NO_WINDOW,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as game:
        ready = queue.Queue(maxsize=1)

        def read_ready_port():
            prefix = b"UNREALAUTOQA_PORT="
            reported = False
            while line := game.stdout.readline(256):
                if not reported and line.startswith(prefix) and line.endswith(b"\n"):
                    value = line[len(prefix):].strip()
                    ready.put(int(value) if value.isdigit() else 0)
                    reported = True
            if not reported:
                ready.put(0)

        reader = threading.Thread(target=read_ready_port, daemon=True)
        reader.start()
        try:
            try:
                port = ready.get(timeout=25)
            except queue.Empty:
                raise Rejected("UE_BRIDGE_STARTUP_TIMEOUT") from None
            if not 1024 <= port <= 65535 or game.poll() is not None:
                raise Rejected("UE_BRIDGE_STARTUP_REJECTED")
            yield loopback_client(port, token, connection_error="UE_CONNECTION_ERROR")
        except Rejected as error:
            if diagnostics is not None and str(error) == "UE_CONNECTION_ERROR":
                # A closing crash connection can precede the engine's dump flush.
                try:
                    game.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            raise
        finally:
            exit_code = game.poll()
            if diagnostics is not None and exit_code is not None:
                diagnostics["game_exit_code"] = exit_code
            if exit_code is None:
                game.terminate()
            game.wait()
            reader.join(timeout=1)
            if diagnostics is not None:
                crashes = sorted(str(path) for path in user_dir.glob("Saved/Crashes/*") if path.is_dir())
                if crashes:
                    diagnostics["crash_directories"] = crashes
                    dumps = sorted(str(path) for path in (user_dir / "Saved" / "Crashes").rglob("*.dmp"))
                    if dumps:
                        diagnostics["crash_dumps"] = dumps


def bridge_health(executable):
    with ue_transport(executable) as call:
        try:
            health = call("/health")
            capabilities = call("/capabilities")
        except Rejected:
            raise Rejected("UE_BRIDGE_CONNECTION_ERROR") from None
    if (not isinstance(health, dict) or health.get("mode") != "UE"
            or health.get("transport_ready") is not True or not isinstance(capabilities, dict)
            or type(health.get("ready")) is not bool or capabilities.get("mode") != "UE"
            or type(capabilities.get("schema_version")) is not int or capabilities["schema_version"] != 1
            or type(capabilities.get("input_enabled")) is not bool):
        raise Rejected("UE_BRIDGE_PROTOCOL_MISMATCH")
    return {
        "health": {"mode": "UE", "ready": health["ready"], "transport_ready": True},
        "capabilities": {"mode": "UE", "schema_version": 1, "input_enabled": capabilities["input_enabled"]},
    }


def bridge_build_files(executable):
    build_files = [executable]
    if len(executable.parents) >= 3:
        paks = executable.parents[2] / "Content" / "Paks"
        build_files += sorted(path for path in paks.glob("*") if path.suffix in {".pak", ".utoc", ".ucas"})
    return {path.name: file_hash(path) for path in build_files}


def wait_for_ue_world(call):
    deadline = time.monotonic() + 15
    while not call("/health")["ready"]:
        if time.monotonic() >= deadline:
            raise Rejected("UE_WORLD_NOT_READY")
        time.sleep(0.05)


def ue_input_verdict(status, expected, before, after, events, capabilities, *, gameplay=False):
    if status == "cancelled":
        return "CANCELLED"
    if status != "completed" or after["held_keys"]:
        raise Rejected("UE_INPUT_INCOMPLETE")
    if not expected or events["lost"]:
        return "INCONCLUSIVE"
    actions = {item["value"] for item in events["events"]
               if item["layer"] == "L2" and item["kind"] == "Triggered"}
    if any(action not in actions for action in expected):
        return "FAIL"
    start, end = before.get("pawn_position"), after.get("pawn_position")
    if any(not isinstance(point, list) or len(point) != 3
           or any(type(value) not in (int, float) or not math.isfinite(value) for value in point)
           for point in (start, end)):
        return "INCONCLUSIVE"
    if "IA_Move" in expected and math.dist(start[:2], end[:2]) <= 0.5:
        return "FAIL"
    if "IA_Jump" in expected:
        if capabilities.get("jump_rise_event") is not True:
            if end[2] - start[2] <= 0.5:
                return "INCONCLUSIVE"
        elif not any(item["layer"] == "L3" and item["kind"] == "JumpRise" and item["value"] == "IA_Jump"
                   for item in events["events"]):
            return "FAIL"
    if gameplay and set(expected) - {"IA_Move", "IA_Jump"}:
        return "INPUT_CONFIRMED"
    return "PASS"


def bridge_test(executable, keys, hold_ms, expected, original=None, cancel=None, steps=None):
    executable = executable.resolve(strict=True)
    build = bridge_build_files(executable)
    if original and original.get("build_files_sha256") != build:
        raise Rejected("REPLAY_BUILD_CHANGED")
    run_id = uuid4().hex
    OUT.mkdir(parents=True, exist_ok=True)
    timeline = OUT / f"{run_id}.jsonl"
    step = {"steps": steps, "expect": expected} if steps is not None else {"keys": keys, "hold_ms": hold_ms, "expect": expected}
    report = {"mode": "UE", "run_id": run_id, "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "build_files_sha256": build, "verdict": "ERROR", "step": step}
    if original:
        report["replay_of"] = original["run_id"]

    def record(phase, data):
        with timeline.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"utc": datetime.now(timezone.utc).isoformat(), "phase": phase,
                                     "data": data}, ensure_ascii=False) + "\n")

    record("start", report)
    phase = "startup"
    diagnostics = {}
    try:
        with ue_transport(executable, diagnostics) as call:
            wait_for_ue_world(call)
            phase = "mapping"
            mappings = call("/mappings")
            capabilities = call("/capabilities")
            native_inputs = None
            if steps is None:
                if expected is None:
                    available = discovered_actions(mappings, {"player_exists": True, "on_ground": True})
                    action = next((name for name in ("IA_Move", "IA_Jump") if name in available),
                                  next(iter(available), None))
                    if not action:
                        raise Rejected("NO_EXECUTABLE_ACTIONS")
                    keys, expected = available[action][0], [action]
                    report["step"] = {"keys": keys, "hold_ms": hold_ms, "expect": expected}
                validate_step(keys, hold_ms, mappings["keys"])
                holds = [{"start_ms": 0, "keys": keys, "hold_ms": hold_ms}]
                duration = hold_ms
            else:
                plan = preview_node(steps, {**mappings, "catalog": {}})
                duration = plan["duration_ms"]
                if capabilities.get("timeline") is True:
                    native_inputs = bridge_timeline(plan)
                    holds = []
                else:
                    holds = bridge_holds(plan, mappings)
                report["input_plan"] = plan
            if original and original.get("mappings") != mappings:
                raise Rejected("REPLAY_MAPPING_CHANGED")
            report["mappings"] = mappings
            session = call("/session", {"schema_version": 1, "input_owner": "MANUAL"})
            envelope = {**session, "request_id": uuid4().hex, "deadline_ms": 3000}
            phase = "command"
            try:
                before = call("/observations")
                report["before"] = before
                if original and (math.dist(original["before"]["pawn_position"], before["pawn_position"]) > 1
                                 or original["before"]["on_ground"] != before["on_ground"]):
                    raise Rejected("REPLAY_INITIAL_STATE_CHANGED")
                record("command", {"step": report["step"], "context": envelope, "before": before})
                status = "completed"
                elapsed = 0

                def wait_gap(ms):
                    end = time.monotonic() + ms / 1000
                    while time.monotonic() < end:
                        if cancel and cancel.is_set():
                            return False
                        call("/session/heartbeat", {**envelope, "request_id": uuid4().hex})
                        time.sleep(max(0, min(0.05, end - time.monotonic())))
                    return not (cancel and cancel.is_set())

                if native_inputs is not None:
                    status = run_command(call, {**envelope, "expected_observation_seq": before["observation_seq"]},
                                         native_inputs, cancel)
                    elapsed = duration
                for hold in holds:
                    if not wait_gap(hold["start_ms"] - elapsed):
                        status = "cancelled"
                        break
                    observed = call("/observations")
                    command = {**envelope, "request_id": uuid4().hex,
                               "expected_observation_seq": observed["observation_seq"]}
                    inputs = {"keys": hold["keys"], "hold_ms": hold["hold_ms"]}
                    status = run_command(call, command, inputs, cancel)
                    elapsed = hold["start_ms"] + hold["hold_ms"]
                    if status != "completed":
                        break
                if status == "completed" and not wait_gap(duration - elapsed):
                    status = "cancelled"
                after = call("/observations")
                events = call(f"/events?after_seq={before['observation_seq']}")
                report.update(command_status=status, after=after, events=events["events"])
                report["verdict"] = ue_input_verdict(status, expected, before, after, events, capabilities)
                if original:
                    observed = [(event["layer"], event["kind"], event["value"]) for event in report["events"]]
                    prior = [(event["layer"], event["kind"], event["value"]) for event in original["events"]]
                    report["comparison"] = {"matches": original["verdict"] == report["verdict"] and prior == observed,
                                            "original_verdict": original["verdict"],
                                            "events_match": prior == observed,
                                            "end_position_delta_cm": math.dist(
                                                original["after"]["pawn_position"], after["pawn_position"])}
            finally:
                call("/session/stop", {**envelope, "request_id": uuid4().hex})
    except (Rejected, OSError, KeyError, ValueError, TypeError) as error:
        code = str(error) if isinstance(error, Rejected) else type(error).__name__
        report.update(verdict="SKIPPED_UNSUPPORTED" if code == "UE_OVERLAPPING_HOLDS_UNSUPPORTED" else "ERROR",
                      failure_phase=phase, error=code)
    report.update(diagnostics)
    record("result", report)
    return save_report(report)


def execute(keys, hold_ms, expect, decision=None, replay_of=None, cancel=None):
    validate_step(keys, hold_ms)
    return execute_input({"keys": keys, "hold_ms": hold_ms}, expect, decision, replay_of, cancel)


def preview_node(steps, mappings):
    try:
        duration, transitions = compile_node(steps, mappings["keys"], mappings["catalog"])
    except ValueError as error:
        raise Rejected(str(error)) from None
    return {"duration_ms": duration, "timeline": [list(edge) for edge in transitions],
            "mapping_revision": mappings["mapping_revision"]}


def single_hold(plan, mappings):
    keys = [key for offset, event, key in plan["timeline"] if offset == 0 and event == "down"]
    duration = plan["duration_ms"]
    if plan["timeline"] != [[0, "down", key] for key in keys] + [[duration, "up", key] for key in keys]:
        raise Rejected("UE_STEP_UNSUPPORTED")
    validate_step(keys, duration, mappings["keys"])
    return {"keys": keys, "hold_ms": duration}


def bridge_holds(plan, mappings):
    holds = []
    start, end = None, None
    keys = []
    pressed = set()
    for offset, event, key in plan["timeline"]:
        if event == "down":
            if not pressed:
                start, end, keys = offset, None, []
            if offset != start or end is not None:
                raise Rejected("UE_OVERLAPPING_HOLDS_UNSUPPORTED")
            keys.append(key)
            pressed.add(key)
        else:
            if end is not None and offset != end:
                raise Rejected("UE_OVERLAPPING_HOLDS_UNSUPPORTED")
            end = offset
            pressed.remove(key)
            if not pressed:
                duration = end - start
                validate_step(keys, duration, mappings["keys"])
                holds.append({"start_ms": start, "keys": keys, "hold_ms": duration})
    return holds


def bridge_timeline(plan):
    transitions = plan["timeline"]
    if len(transitions) > 96:
        raise Rejected("UE_TIMELINE_LIMIT")
    pressed = {}
    for offset, kind, key in transitions:
        if kind == "down":
            pressed[key] = offset
        elif not 20 <= offset - pressed.pop(key) <= 2000:
            raise Rejected("HOLD_MS_OUT_OF_RANGE")
    return {"timeline": transitions, "duration_ms": plan["duration_ms"]}


def run_command(call, envelope, inputs, cancel=None):
    payload = {**envelope, **inputs}
    status_route = f"/commands/{envelope['request_id']}"
    try:
        call("/commands", payload)
    except Rejected as error:
        if str(error) != "UE_CONNECTION_ERROR":
            raise
        try:
            call(status_route)
        except Rejected as status_error:
            if str(status_error) != "UNKNOWN_REQUEST":
                raise
            call("/commands", payload)
    deadline = time.monotonic() + 4
    heartbeat_at = time.monotonic() + 0.2
    while True:
        now = time.monotonic()
        if cancel and cancel.is_set():
            call("/input/release-all", {**envelope, "request_id": uuid4().hex})
        status = call(status_route)["status"]
        if status != "running":
            return status
        if now >= deadline:
            raise Rejected("COMMAND_TIMEOUT")
        if now >= heartbeat_at:
            call("/session/heartbeat", {**envelope, "request_id": uuid4().hex})
            heartbeat_at = now + 0.2
        time.sleep(0.02)


def comparison(original, report):
    fields = {
        "verdict": (original["verdict"], report["verdict"]),
        "position": (original["after"]["position"], report["after"]["position"]),
        "jumps": (original["after"]["jumps"], report["after"]["jumps"]),
        "input_plan": (original.get("input_plan"), report.get("input_plan")),
        "events": tuple([[e["layer"], e["kind"], e["value"]] for e in item["events"]]
                        for item in (original, report)),
    }
    differences = {name: {"original": first, "replay": second}
                   for name, (first, second) in fields.items() if first != second}
    return {"matches": not differences, "differences": differences}


def evaluate(status, expect, before, after, events):
    if status != "completed" or after.get("held_keys"):
        return "ERROR"
    if not expect or events is None or "held_keys" not in after:
        return "INCONCLUSIVE"
    if not any(event["kind"] == "Triggered" and event["value"] == expect for event in events):
        return "FAIL"
    field = {"IA_Jump": "jumps", "IA_Move": "position"}.get(expect)
    previous, current = before.get(field), after.get(field)
    if type(previous) not in (int, float) or type(current) not in (int, float):
        return "INCONCLUSIVE"
    return "PASS" if current > previous else "FAIL"


def execute_input(inputs, expect, decision=None, replay_of=None, cancel=None, original=None, owner=None):
    if expect not in ("", *MAPPINGS.values()):
        raise Rejected("UNKNOWN_EXPECTED_ACTION")
    run_id = uuid4().hex
    start = datetime.now(timezone.utc).isoformat()
    with transport() as call:
        health = call("/health")
        mappings = call("/mappings")
        if decision and decision.get("mappings") != mappings:
            raise Rejected("STALE_DECISION")
        if original and original["mappings"] != mappings:
            raise Rejected("REPLAY_MAPPING_CHANGED")
        plan = preview_node(inputs["steps"], mappings) if "steps" in inputs else None
        owner = owner or ("DECISIONS" if decision else "SCRIPTED" if original else "MANUAL")
        session = call("/session", {"schema_version": 1, "input_owner": owner})
        envelope = {**session, "request_id": uuid4().hex, "deadline_ms": 3000}
        before = call("/observations")
        envelope["expected_observation_seq"] = before["observation_seq"]
        try:
            status = run_command(call, envelope, inputs, cancel)
        finally:
            call("/input/release-all", {**envelope, "request_id": uuid4().hex})
            after = call("/observations")
            events = call("/events?after_seq=0")["events"]
            call("/session/stop", {**envelope, "request_id": uuid4().hex})

    verdict = evaluate(status, expect, before, after, events)
    report = {"mode": "MOCK", "run_id": run_id, "started_at_utc": start, "build": health["build"],
              "replay_of": replay_of, "verdict": verdict, "command_status": status,
              "step": {**inputs, "expect": expect}, "decision": decision,
              "command": envelope, "mappings": mappings, "before": before, "after": after, "events": events}
    if plan is not None:
        report["input_plan"] = plan
    if original:
        report["comparison"] = comparison(original, report)
    return save_report(report)


def save_report(report):
    report["runtime_sha256"] = runtime_fingerprint()
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / report["run_id"]
    data = json.dumps(report, indent=2, ensure_ascii=False)
    path.with_suffix(".json").write_text(data + "\n", encoding="utf-8")
    path.with_suffix(".html").write_text(
        '<!doctype html><html lang="ko"><meta charset="utf-8"><title>Unreal Auto QA</title>'
        f'<h1>{"UE QA" if report["mode"] == "UE" else "모의 QA"} · {html.escape(report["verdict"])}</h1>'
        f'<pre>{html.escape(data)}</pre></html>',
        encoding="utf-8",
    )
    return report


def decide(allowed, live=False, *, catalog=None, scope="selected_actions", selected_keys=None):
    catalog = read_catalog(None) if catalog is None else catalog
    with transport() as call:
        mappings = call("/mappings")
        call("/session", {"schema_version": 1, "input_owner": "DECISIONS"})
        observation = call("/observations")
    available = action_candidates(allowed, mappings, catalog, scope, selected_keys, observation)
    choices = {action: description for action, (_, _, description) in available.items()}
    choices["WAIT"] = "Do not send input"
    if live:
        try:
            key = os.environ.get("OPENAI_API_KEY", "").strip() or load_key()
        except OSError:
            raise Rejected("KEY_STORAGE_ERROR") from None
        status, choice = request_choice(
            "Synthetic QA session at the initial state. Choose an allowed gameplay action.", choices,
            key=key)
    else:
        status, choice = classify_answer({
            "answers": [{"type": "choice", "name": "next_action", "choice": next(iter(available))}]
        }, choices)
    decision = {"source": "openai" if live else "mock", "status": status, "choice": choice,
                "candidates": list(choices), "mappings": mappings}
    if status != "OK" or choice == "WAIT":
        return save_report({"mode": "MOCK", "run_id": uuid4().hex, "step": None,
                            "verdict": "INCONCLUSIVE" if status == "OK" else "ERROR",
                            "decision": decision, "events": []})
    keys, expect, _ = available[choice]
    return execute(keys, 100, expect, decision=decision)


def explore(allowed, max_calls, max_seconds, live=False, *, max_input_tokens=100_000,
            max_estimated_usd=Decimal("0.01"), input_usd_per_million=Decimal("0.10"),
            catalog=None, scope="selected_actions", selected_keys=None, executable=None, cancel=None,
            objective=None, checks=()):
    if objective is not None:
        objective = objective.strip()
        if not 1 <= len(objective) <= 240:
            raise Rejected("INVALID_TEST_OBJECTIVE")
    automatic = executable is not None and catalog is None
    check_results = {}
    for check in checks:
        actions = check.split("+")
        if (len(actions) > MAX_SIMULTANEOUS_INPUTS or len(actions) != len(set(actions)) or any(
                action == "WAIT" or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}(?:@[A-Za-z0-9_]{1,64})?", action)
                for action in actions)):
            raise Rejected("INVALID_TEST_CHECK")
        check_results.setdefault("+".join(sorted(actions)), "UNVERIFIED")
    if check_results and (not automatic or len(check_results) > 20):
        raise Rejected("INVALID_TEST_CHECK")
    if max_calls is None:
        max_calls = len(check_results) or 2
    if (not 1 <= max_calls <= 1000 or not 1 <= max_seconds <= 600
            or not 1 <= max_input_tokens <= 100_000_000
            or not max_estimated_usd.is_finite() or not Decimal("0.000001") <= max_estimated_usd <= 1000
            or not input_usd_per_million.is_finite() or not Decimal("0.000001") <= input_usd_per_million <= 1000):
        raise Rejected("INVALID_EXPLORATION_BUDGET")
    attempts = []
    visits = {}
    input_tokens = 0
    reserved_tokens = 0
    reserve = INPUT_TOKEN_RESERVE
    retries = 0
    exhausted_states = {}
    rate = input_usd_per_million / 1_000_000
    if automatic and not live:
        raise Rejected("DECISIONS_REQUIRED")
    catalog = read_catalog(None) if catalog is None and not automatic else catalog
    outcome = "BUDGET_EXHAUSTED"
    started = time.monotonic()

    def current_candidates(mappings, observation):
        available = discovered_actions(mappings, observation, key_variants=bool(check_results))
        if not check_results:
            return available
        candidates = {}
        for check, verdict in check_results.items():
            actions = check.split("+")
            if verdict == "UNVERIFIED" and all(action in available for action in actions):
                keys = [available[action][0][0] for action in actions]
                if len(set(keys)) == len(keys):
                    expected = "+".join(available[action][1] for action in actions)
                    candidates[check] = (keys, expected, f"Press {' + '.join(keys)}.")
        return candidates

    with (ue_transport(executable) if executable else transport()) as call:
        if executable:
            wait_for_ue_world(call)
        capabilities = call("/capabilities") if automatic else {}
        session = call("/session", {"schema_version": 1, "input_owner": "DECISIONS"})
        envelope = {**session, "deadline_ms": 3000}
        identity_fields = ("session_id", "world_generation", "input_owner", "owner_revision")

        def owns_input(observation):
            return all(observation.get(name) == envelope[name] for name in identity_fields)

        def heartbeat():
            call("/session/heartbeat", {**envelope, "request_id": uuid4().hex})

        def delay_with_lease(duration):
            end = time.monotonic() + duration
            while time.monotonic() < end and not (cancel and cancel.is_set()):
                if executable:
                    heartbeat()
                time.sleep(min(0.15 if executable else duration, max(0, end - time.monotonic())))

        initial = call("/observations")
        initial_mappings = call("/mappings")
        deadline = time.monotonic() + max_seconds
        try:
            while len(attempts) < max_calls and time.monotonic() < deadline:
                if cancel and cancel.is_set():
                    outcome = "CANCELLED"
                    break
                mappings = call("/mappings")
                before = call("/observations")
                if automatic:
                    if not owns_input(before) or before.get("lease_lost"):
                        outcome = "STALE_DECISION"
                        break
                    if before["expected_mapping_revision"] != mappings["mapping_revision"]:
                        outcome = "STALE_DECISION"
                        break
                    envelope["expected_mapping_revision"] = mappings["mapping_revision"]
                available = (current_candidates(mappings, before) if automatic
                             else action_candidates(allowed, mappings, catalog, scope, selected_keys, before))
                if automatic and available:
                    tested = {(item["choice"], item["mapping_revision"]) for item in attempts if "verdict" in item}
                    available = {action: data for action, data in available.items()
                                 if (action, mappings["mapping_revision"]) not in tested}
                    if not available:
                        outcome = "COVERAGE_COMPLETE"
                        break
                if not available:
                    outcome = "NO_EXECUTABLE_ACTIONS"
                    break
                choices = {action: description for action, (_, _, description) in available.items()}
                choices["WAIT"] = "Do not send input"
                signature = (mappings["mapping_revision"], before.get("on_ground"), tuple(sorted(choices.items())))
                if automatic and signature in exhausted_states:
                    outcome = exhausted_states[signature]
                    break
                metadata = {}
                decision_started = time.monotonic()
                if live:
                    fields = (("on_ground",) if automatic else
                              ("player_exists", "on_ground", "pawn_position", "pawn_velocity") if executable
                              else ("position", "jumps"))
                    state = json.dumps({name: before.get(name) for name in fields}, separators=(",", ":"))
                    prompt = f"Select the next gameplay test. Choose WAIT if none is suitable. State: {state}"
                    if objective is not None:
                        goal = json.dumps(objective, ensure_ascii=False)
                        prompt += f" QA objective: {goal}. Choose an action that advances this objective, or WAIT."
                    estimate = max(reserve, len(choice_body(prompt, choices)) + 128)
                    projected = input_tokens + reserved_tokens + estimate
                    if projected > max_input_tokens or projected * rate > max_estimated_usd:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining < 0.1:
                        break
                    if not attempts:
                        try:
                            key = load_key() if executable else os.environ.get("OPENAI_API_KEY", "").strip() or load_key()
                        except OSError:
                            raise Rejected("KEY_STORAGE_ERROR") from None
                        if not key:
                            raise Rejected("API_KEY_MISSING")
                    if executable:
                        with ThreadPoolExecutor(max_workers=1) as worker:
                            pending = worker.submit(request_choice, prompt, choices, key=key, metadata=metadata,
                                                    timeout=min(10, remaining))
                            while not pending.done() and not (cancel and cancel.is_set()):
                                try:
                                    heartbeat()
                                except Rejected as error:
                                    if not automatic or str(error) != "MAPPING_CHANGED":
                                        raise
                                    break
                                time.sleep(0.15)
                            status, choice = pending.result()
                    else:
                        status, choice = request_choice(prompt, choices, key=key, metadata=metadata,
                                                        timeout=min(10, remaining))
                    used = metadata.get("input_tokens")
                    if used is None:
                        reserved_tokens += estimate
                    else:
                        input_tokens += used
                        reserve = max(reserve, used)
                else:
                    selected = min(available, key=lambda action: visits.get(action, 0))
                    status, choice = classify_answer({
                        "answers": [{"type": "choice", "name": "next_action", "choice": selected}]
                    }, choices)
                attempt = {"status": status, "choice": choice, "candidates": list(choices), "before": before}
                attempt["latency_ms"] = round((time.monotonic() - decision_started) * 1000, 2)
                if live:
                    attempt["usage"] = metadata
                    attempt["estimated_input_tokens_before_call"] = estimate
                attempts.append(attempt)
                if cancel and cancel.is_set():
                    outcome = "CANCELLED"
                    break
                if status != "OK" or choice not in choices:
                    outcome = status if status != "OK" else "DECISION_INVALID"
                    retryable = status in ("API_TRANSIENT_ERROR", "API_NETWORK_ERROR", "API_RATE_OR_QUOTA")
                    if live and not automatic and retryable and not metadata.get("quota_exhausted") and retries < 2:
                        delay = max(LIVE_INTERVAL_SECONDS * (2 ** retries),
                                    metadata.get("retry_after_seconds", 0))
                        if len(attempts) < max_calls and time.monotonic() + delay < deadline:
                            attempt["retry_delay_seconds"] = delay
                            retries += 1
                            delay_with_lease(delay)
                            continue
                    break
                if live and metadata.get("input_tokens") is None:
                    outcome = "API_USAGE_UNAVAILABLE"
                    break
                if (input_tokens + reserved_tokens > max_input_tokens
                        or (input_tokens + reserved_tokens) * rate > max_estimated_usd):
                    outcome = "BUDGET_EXHAUSTED"
                    break
                retries = 0
                outcome = "BUDGET_EXHAUSTED"
                if time.monotonic() >= deadline:
                    break
                current = call("/observations")
                current_mappings = call("/mappings")
                if automatic:
                    if not owns_input(current) or current.get("lease_lost"):
                        outcome = "STALE_DECISION"
                        break
                    eligible = current_candidates(current_mappings, current)
                    changed = (current["expected_mapping_revision"] != mappings["mapping_revision"]
                               or current_mappings["mapping_revision"] != mappings["mapping_revision"]
                               or (choice != "WAIT" and eligible.get(choice) != available.get(choice)))
                    if changed or current["observation_seq"] != before["observation_seq"]:
                        outcome = "STALE_DECISION"
                        attempt["discarded"] = outcome
                        exhausted_states[signature] = outcome
                        if changed and current["expected_mapping_revision"] == current_mappings["mapping_revision"]:
                            continue
                        break
                    if choice == "WAIT":
                        exhausted_states[signature] = "WAIT_NO_STATE_CHANGE"
                    before = current
                elif current != before or current_mappings != mappings:
                    outcome = "STALE_DECISION"
                    break
                if choice != "WAIT":
                    if automatic:
                        attempt["input_before"] = before
                    visits[choice] = visits.get(choice, 0) + 1
                    keys, action, _ = available[choice]
                    command = {**envelope, "request_id": uuid4().hex,
                               "expected_observation_seq": before["observation_seq"]}
                    attempt["input"] = {"keys": keys, "hold_ms": 100}
                    if executable:
                        attempt["input_at_ms"] = round((time.monotonic() - started) * 1000)
                    attempt["command_status"] = run_command(call, command, attempt["input"], cancel)
                    if attempt["command_status"] != "completed":
                        outcome = "CANCELLED" if cancel and cancel.is_set() else "INPUT_INTERRUPTED"
                        break
                attempt["after"] = call("/observations")
                if automatic and choice != "WAIT":
                    evidence = call(f"/events?after_seq={before['observation_seq']}")
                    attempt["mapping_revision"] = mappings["mapping_revision"]
                    attempt["verdict"] = ue_input_verdict(attempt["command_status"], action.split("+"), before,
                                                         attempt["after"], evidence, capabilities, gameplay=True)
                    if attempt["verdict"] == "PASS" and any(
                            item.startswith("IA_Move@") for item in choice.split("+")):
                        attempt["verdict"] = "INPUT_CONFIRMED"
                    if check_results:
                        check_results[choice] = attempt["verdict"]
                        if "UNVERIFIED" not in check_results.values():
                            outcome = "COVERAGE_COMPLETE"
                            break
                    elif attempt["verdict"] == "FAIL":
                        outcome = "ACTION_FAILED"
                        break
                if len(attempts) < max_calls:
                    interval = LIVE_INTERVAL_SECONDS if live else 0.5
                    delay_with_lease(min(interval, max(0, deadline - time.monotonic())))
        except KeyboardInterrupt:
            outcome = "CANCELLED"
        except Rejected as error:
            outcome = str(error)
        finally:
            after = call("/observations")
            same_owner = owns_input(after) if automatic else after["owner_revision"] == envelope["owner_revision"]
            if same_owner:
                cleanup = {**envelope, "expected_mapping_revision": after["expected_mapping_revision"],
                           "request_id": uuid4().hex}
                call("/input/release-all", cleanup)
                after = call("/observations")
            event_window = call("/events?after_seq=0")
            events = event_window["events"]
            if same_owner:
                call("/session/stop", {**cleanup, "request_id": uuid4().hex})
    build = ({"build_files_sha256": bridge_build_files(executable.resolve(strict=True))} if executable
             else {"build": "mock-v1"})
    latencies = sorted(attempt["latency_ms"] for attempt in attempts)
    percentiles = {f"p{p}": latencies[math.ceil(len(latencies) * p / 100) - 1] for p in (50, 95, 99)} if latencies else {}
    verdict = "INCONCLUSIVE" if outcome == "BUDGET_EXHAUSTED" else "ERROR"
    if automatic:
        if outcome == "ACTION_FAILED":
            verdict = "FAIL"
        elif outcome == "CANCELLED":
            verdict = "CANCELLED"
        elif outcome in ("NO_EXECUTABLE_ACTIONS", "WAIT_NO_STATE_CHANGE", "COVERAGE_COMPLETE"):
            verdict = "INCONCLUSIVE"
        if outcome in ("BUDGET_EXHAUSTED", "COVERAGE_COMPLETE", "WAIT_NO_STATE_CHANGE") and any(item.get("verdict") == "PASS" for item in attempts):
            verdict = "PASS"
    if check_results and outcome in ("BUDGET_EXHAUSTED", "COVERAGE_COMPLETE",
                                     "WAIT_NO_STATE_CHANGE", "NO_EXECUTABLE_ACTIONS"):
        results = set(check_results.values())
        verdict = "FAIL" if "FAIL" in results else "PASS" if results == {"PASS"} else "INCONCLUSIVE"
    return save_report({"mode": "UE" if executable else "MOCK", "run_id": uuid4().hex, **build, "step": None,
                        "verdict": verdict,
                        "exploration": {"source": "openai" if live else "mock", "outcome": outcome,
                                        "objective": objective,
                                        "checks": check_results,
                                        "max_calls": max_calls, "max_seconds": max_seconds,
                                        "elapsed_seconds": round(time.monotonic() - started, 3),
                                        "latency_ms": percentiles,
                                        "scope": scope,
                                        "max_input_tokens": max_input_tokens,
                                        "max_estimated_usd": str(max_estimated_usd),
                                        "input_usd_per_million": str(input_usd_per_million),
                                        "reported_input_tokens": input_tokens,
                                        "estimated_unreported_input_tokens": reserved_tokens,
                                        "estimated_usd": str((input_tokens + reserved_tokens) * rate),
                                        "attempts": attempts},
                        "before": initial, "after": after, "events": events, "mappings": initial_mappings,
                        "events_lost": bool(event_window.get("lost"))})


def replay_exploration(saved, executable=None):
    attempts = saved["exploration"]["attempts"]
    if not isinstance(attempts, list) or len(attempts) > 1000:
        raise Rejected("INVALID_REPLAY")
    completed = [item for item in attempts if item.get("command_status") == "completed"]
    inputs = [item["input"] for item in completed]
    if executable:
        build = bridge_build_files(executable.resolve(strict=True))
        if saved.get("build_files_sha256") != build:
            raise Rejected("REPLAY_BUILD_CHANGED")
        offsets = [item.get("input_at_ms", 0 if len(completed) == 1 else None) for item in completed]
        if any(type(value) is not int or not 0 <= value <= 610_000 for value in offsets) or offsets != sorted(offsets):
            raise Rejected("REPLAY_INPUT_TIMING_MISSING")
    with (ue_transport(executable) if executable else transport()) as call:
        if executable:
            wait_for_ue_world(call)
        mappings = call("/mappings")
        if mappings != saved["mappings"]:
            raise Rejected("REPLAY_MAPPING_CHANGED")
        for item in inputs:
            validate_step(item["keys"], item["hold_ms"], mappings["keys"] if executable else None)
        session = call("/session", {"schema_version": 1, "input_owner": "SCRIPTED"})
        envelope = {**session, "deadline_ms": 3000}
        before = call("/observations")
        replayed = []
        replay_started = time.monotonic()
        try:
            if executable:
                check_replay_start(saved["before"], before)
            for index, item in enumerate(inputs):
                if executable:
                    at = replay_started + (offsets[index] - offsets[0]) / 1000
                    while time.monotonic() < at:
                        call("/session/heartbeat", {**envelope, "request_id": uuid4().hex})
                        time.sleep(max(0, min(0.15, at - time.monotonic())))
                command = {**envelope, "request_id": uuid4().hex}
                status = run_command(call, command, {"keys": item["keys"], "hold_ms": item["hold_ms"]})
                replayed.append({"input": item, "command_status": status})
                if status != "completed":
                    break
        finally:
            call("/input/release-all", {**envelope, "request_id": uuid4().hex})
            after = call("/observations")
            event_window = call("/events?after_seq=0")
            events = event_window["events"]
            call("/session/stop", {**envelope, "request_id": uuid4().hex})
    report = {"mode": "UE" if executable else "MOCK", "run_id": uuid4().hex, "step": None,
              "verdict": "INCONCLUSIVE", "replay_of": saved["run_id"], "before": before,
              "after": after, "events": events, "mappings": mappings,
              "exploration": {"source": "replay", "attempts": replayed}}
    if len(replayed) != len(inputs) or any(item["command_status"] != "completed" for item in replayed):
        report["verdict"] = "ERROR"
    if executable:
        report["build_files_sha256"] = build
        report["events_lost"] = bool(event_window.get("lost"))
        events_match = compare_ue_events(saved, report)
        report["comparison"] = {"matches": report["verdict"] != "ERROR" and events_match, "events_match": events_match}
    else:
        report["build"] = "mock-v1"
        report["comparison"] = comparison(saved, report)
    return save_report(report)


def check_replay_start(original, current):
    point = original.get("pawn_position")
    if (not isinstance(point, list) or len(point) != 3
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in point)
            or math.dist(point, current["pawn_position"]) > 1
            or original.get("on_ground") != current.get("on_ground")):
        raise Rejected("REPLAY_INITIAL_STATE_CHANGED")


def compare_ue_events(original, report):
    def trace(item):
        return [(entry["layer"], entry["kind"], entry["value"]) for entry in item["events"]]
    return not original.get("events_lost") and not report.get("events_lost") and trace(original) == trace(report)


def run_scenario(document, original=None, *, executable=None, catalog=None):
    build = bridge_build_files(executable.resolve(strict=True)) if executable else None
    if executable and original and original.get("build_files_sha256") != build:
        raise Rejected("REPLAY_BUILD_CHANGED")
    with (ue_transport(executable) if executable else transport()) as call:
        if executable:
            wait_for_ue_world(call)
        mappings = call("/mappings")
        if executable:
            mappings["catalog"] = {
                name: row["input_actions"][0] for name, row in catalog.items()
                if row["semantic_status"] in ("USER_CONFIRMED", "ADAPTER_VERIFIED")
                and len(row["input_actions"]) == 1
            }
        ready = call("/health")["ready"]
        if original and original["mappings"] != mappings:
            raise Rejected("REPLAY_MAPPING_CHANGED")
        nodes = scenario_nodes(document, mappings)
        plans = [preview_node(steps, mappings) for _, steps, _ in nodes]
        preconditions = document.get("preconditions", [])
        if not isinstance(preconditions, list):
            raise Rejected("INVALID_PRECONDITIONS")
        for item in preconditions:
            if isinstance(item, dict) and set(item) == {"input_action_available"}:
                logical = item["input_action_available"]
                if not isinstance(logical, str) or mappings["catalog"].get(logical) not in mappings["keys"].values():
                    raise Rejected("SCENARIO_PRECONDITION_UNMET")
            elif item == "game_ready":
                if not ready:
                    raise Rejected("SCENARIO_PRECONDITION_UNMET")
            elif item != "player_exists":
                raise Rejected("UNSUPPORTED_SCENARIO_PRECONDITION")
        session = call("/session", {"schema_version": 1, "input_owner": "SCRIPTED"})
        envelope = {**session, "deadline_ms": 3000}
        initial = call("/observations")
        if executable and original:
            try:
                check_replay_start(original["before"], initial)
            except (Rejected, KeyError, AttributeError, TypeError):
                call("/session/stop", {**envelope, "request_id": uuid4().hex})
                raise
        if "player_exists" in preconditions and not initial.get("player_exists"):
            call("/session/stop", {**envelope, "request_id": uuid4().hex})
            raise Rejected("SCENARIO_PRECONDITION_UNMET")
        attempts = []
        verdict = "PASS"
        try:
            for (name, steps, assertions), plan in zip(nodes, plans):
                before = call("/observations")
                seq = before["observation_seq"]
                command = {**envelope, "request_id": uuid4().hex, "expected_observation_seq": seq}
                inputs = {"steps": steps}
                if executable:
                    inputs = single_hold(plan, mappings)
                status = run_command(call, command, inputs)
                after = call("/observations")
                window = call(f"/events?after_seq={seq}")
                checks = evaluate_assertions(assertions, before, after, None if window.get("lost") else window["events"],
                                              mappings["catalog"], status)
                outcomes = {check["verdict"] for check in checks}
                outcome = ("CANCELLED" if executable and status == "cancelled" else
                           "ERROR" if status != "completed" else "FAIL" if "FAIL" in outcomes
                           else "PASS" if outcomes == {"PASS"} else "INCONCLUSIVE")
                attempts.append({"id": name, "request_id": command["request_id"], "before": before,
                                 "after": after, "assertions": checks, "verdict": outcome})
                if outcome in ("FAIL", "ERROR", "CANCELLED"):
                    verdict = outcome
                    break
                if outcome == "INCONCLUSIVE":
                    verdict = outcome
        finally:
            call("/input/release-all", {**envelope, "request_id": uuid4().hex})
            after = call("/observations")
            final_events = call("/events?after_seq=0")
            events = final_events["events"]
            call("/session/stop", {**envelope, "request_id": uuid4().hex})
    report = {"mode": "UE" if executable else "MOCK", "run_id": uuid4().hex, "step": None,
              "verdict": verdict, "scenario": document, "attempts": attempts, "mappings": mappings,
              "before": initial, "after": after, "events": events}
    if executable:
        report["build_files_sha256"] = build
        report["events_lost"] = bool(final_events.get("lost"))
    else:
        report["build"] = "mock-v1"
    if original:
        report["replay_of"] = original["run_id"]
        if executable:
            observed_steps = lambda item: [(entry["id"], entry["verdict"], entry["assertions"])
                                           for entry in item["attempts"]]
            steps_match = observed_steps(original) == observed_steps(report)
            events_match = compare_ue_events(original, report)
            report["comparison"] = {"matches": original["verdict"] == verdict and steps_match and events_match,
                                    "steps_match": steps_match, "events_match": events_match}
        else:
            report["comparison"] = comparison(original, report)
    return save_report(report)


def replay(path):
    try:
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
        step = saved["step"]
        if saved["mode"] != "MOCK" or saved["build"] != "mock-v1":
            raise Rejected("INCOMPATIBLE_REPLAY")
        if saved.get("runtime_sha256", runtime_fingerprint()) != runtime_fingerprint():
            raise Rejected("REPLAY_BUILD_CHANGED")
        if "exploration" in saved:
            return replay_exploration(saved)
        if "scenario" in saved:
            return run_scenario(saved["scenario"], original=saved)
        if "steps" in step:
            inputs = {"steps": step["steps"]}
        else:
            validate_step(step["keys"], step["hold_ms"])
            inputs = {"keys": step["keys"], "hold_ms": step["hold_ms"]}
        if "catalog" not in saved["mappings"]:
            saved["mappings"] = {**saved["mappings"], "catalog": MOCK_CATALOG}
        return execute_input(inputs, step["expect"], replay_of=saved["run_id"], original=saved)
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        raise Rejected("INVALID_REPLAY") from None


def make_panel_server(port, executable=None):
    lock = threading.Lock()
    cancel = threading.Event()
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path != "/" or self.headers.get("Host") != host:
                return self.send_error(404)
            page = Path(__file__).with_name("qa_panel.html").read_text(encoding="utf-8")
            data = page.replace("__PANEL_TOKEN__", token).replace(
                "__PANEL_MODE__", "UE 게임 연결 준비" if executable else "실행할 UE 게임을 지정하세요."
            ).replace("__AI_DISABLED__", "" if executable else "disabled").replace(
                "__GAME_HINT_HIDDEN__", "hidden" if executable else "").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if (self.headers.get("Host") != host or self.headers.get("Origin") != f"http://{host}"
                    or not secrets.compare_digest(self.headers.get("X-QA-Token", "").encode(), token.encode())):
                return reply(self, 403, {"error": "FORBIDDEN"})
            if self.path not in ("/run", "/cancel", "/projects",
                                 "/project/doctor", "/project/preview", "/project/apply", "/project/recover"):
                return reply(self, 404, {"error": "UNKNOWN_ROUTE"})
            try:
                body = read_json(self)
                if self.path == "/projects":
                    return reply(self, 200, {"projects": project_candidates()})
                if self.path.startswith("/project/"):
                    project = local_path(body.get("project"))
                    if self.path == "/project/doctor":
                        return reply(self, 200, inspect_project(project, ROOT / "spikes/check_environment.ps1"))
                    if self.path == "/project/recover":
                        return reply(self, 200, recover_project(project))
                    mode = body.get("mode")
                    if mode not in ("install", "remove"):
                        raise Rejected("INVALID_PLUGIN_OPERATION")
                    source = local_path(body.get("source")) if mode == "install" else None
                    if self.path == "/project/preview":
                        return reply(self, 200, plugin_preview(project, source))
                    approval = body.get("approval")
                    if not isinstance(approval, str) or len(approval) != 64:
                        raise Rejected("PLUGIN_APPROVAL_REQUIRED")
                    return reply(self, 200, install_plugin(project, source, approval))
                if not executable:
                    raise Rejected("UE_GAME_REQUIRED")
                if self.path == "/cancel":
                    cancel.set()
                    return reply(self, 200, {"status": "CANCEL_REQUESTED"})
                if set(body) != {"max_calls"} or type(body["max_calls"]) is not int or not 1 <= body["max_calls"] <= 20:
                    raise Rejected("INVALID_EXPLORATION_BUDGET")
                if not lock.acquire(blocking=False):
                    return reply(self, 409, {"error": "INPUT_BUSY"})
                try:
                    cancel.clear()
                    result = explore(None, body["max_calls"], 60, live=True, executable=executable, cancel=cancel)
                finally:
                    lock.release()
                reply(self, 200, result)
            except (ValueError, OSError) as error:
                reply(self, 400, {"error": str(error) if isinstance(error, Rejected) else "BAD_REQUEST"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    host = f"127.0.0.1:{server.server_port}"
    return server


def panel(port, open_browser=False, executable=None):
    with make_panel_server(port, executable) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"UE QA panel: {url}", flush=True)
        if open_browser:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def register_key():
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            key = getpass("OpenAI API key (input hidden): ").strip()
    except (GetPassWarning, EOFError, KeyboardInterrupt, OSError):
        raise Rejected("KEY_INPUT_CANCELLED_OR_UNAVAILABLE") from None
    if not 1 <= len(key) <= 512 or any(not 33 <= ord(c) <= 126 for c in key):
        raise Rejected("INVALID_API_KEY")
    try:
        save_key(key)
    except OSError:
        raise Rejected("KEY_STORAGE_ERROR") from None
    print("KEY_SAVED_LOCALLY")


def decimal_arg(value):
    try:
        return Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("a decimal number is required") from None


def main():
    if FROZEN and len(sys.argv) == 1:
        panel(18500, open_browser=True)
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ai = commands.add_parser("ai-test")
    ai.add_argument("game", type=Path, help="Packaged Win64 game executable")
    ai.add_argument("--objective", help="Gameplay test objective in natural language (up to 240 characters)")
    ai.add_argument("--check", action="append", default=[],
                    help="Required Unreal action ID. Repeat for separate checks or join IDs with + for simultaneous input.")
    ai.add_argument("--max-calls", type=int, help="Call cap (defaults to the number of checks, or 2 for exploration)")
    ai.add_argument("--max-seconds", type=int, default=30)
    actions = commands.add_parser("list-actions", help="List testable Unreal action IDs without a Decisions call")
    actions.add_argument("game", type=Path, help="Packaged Win64 game executable")
    actions.add_argument("--with-keys", action="store_true", help="Include the keyboard mapping for each action")
    actions.add_argument("--from-report", type=Path, help="Read the mapping captured by the QA build smoke test")
    ui = commands.add_parser("panel")
    ui.add_argument("--port", type=int, default=18500)
    ui.add_argument("--open-browser", action="store_true")
    ui.add_argument("--game", type=Path)
    commands.add_parser("register-key")
    bridge = commands.add_parser("bridge-health")
    bridge.add_argument("executable", type=Path)
    smoke = commands.add_parser("_build-smoke")
    smoke.add_argument("executable", type=Path)
    fingerprint = commands.add_parser("project-fingerprint")
    fingerprint.add_argument("project", type=Path)
    fingerprint.add_argument("--engine-build", required=True)
    fingerprint.add_argument("--metadata-only", action="store_true")
    copy_inputs = commands.add_parser("project-copy-inputs")
    copy_inputs.add_argument("project", type=Path)
    copy_inputs.add_argument("destination", type=Path)
    plugin = commands.add_parser("plugin-stage")
    plugin.add_argument("project", type=Path)
    plugin.add_argument("source", type=Path)
    plugin.add_argument("--approve")
    remove = commands.add_parser("plugin-remove")
    remove.add_argument("project", type=Path)
    remove.add_argument("--approve")
    recovery = commands.add_parser("plugin-recover")
    recovery.add_argument("project", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "list-actions":
            if args.from_report:
                mappings = json.loads(args.from_report.read_text(encoding="utf-8"))["mappings"]
            else:
                with ue_transport(args.game) as call:
                    wait_for_ue_world(call)
                    mappings = call("/mappings")
            actions = discovered_actions(mappings, {"player_exists": True, "on_ground": True},
                                         key_variants=args.with_keys)
            if args.with_keys:
                modifiers = {item["key"]: item["modifiers"] for item in mappings.get("effective_mappings", [])
                             if item["modifiers"]}
                result = {name: {"key": keys[0], "modifiers": modifiers.get(keys[0], [])}
                          for name, (keys, _, _) in sorted(actions.items())}
            else:
                result = sorted(actions)
            print(json.dumps(result, separators=(",", ":")))
            return 0
        if args.command == "bridge-health":
            print(json.dumps(bridge_health(args.executable), indent=2))
            return 0
        if args.command == "_build-smoke":
            result = bridge_test(args.executable, None, 150, None)
            print(f'UE {result["verdict"]}: {OUT / result["run_id"]}.json')
            return 0 if result["verdict"] == "PASS" else 2
        if args.command == "project-fingerprint":
            print(json.dumps(project_fingerprint(args.project, args.engine_build, metadata_only=args.metadata_only),
                             ensure_ascii=False))
            return 0
        if args.command == "project-copy-inputs":
            print(json.dumps(copy_project_inputs(args.project, args.destination), ensure_ascii=False))
            return 0
        if args.command == "plugin-recover":
            print(json.dumps(recover_project(args.project), ensure_ascii=False))
            return 0
        if args.command in ("plugin-stage", "plugin-remove"):
            source = args.source if args.command == "plugin-stage" else None
            result = (install_plugin(args.project, source, args.approve) if args.approve
                      else plugin_preview(args.project, source))
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.command == "register-key":
            register_key()
            return 0
        if args.command == "panel":
            panel(args.port, args.open_browser, args.game)
            return 0
        result = explore(None, args.max_calls, args.max_seconds, live=True, executable=args.game,
                         objective=args.objective, checks=args.check)
    except Rejected as error:
        print(f"ERROR: {error}")
        return 2
    except (OSError, json.JSONDecodeError) as error:
        print(f"ERROR: {type(error).__name__}")
        return 2
    print(f'{result["mode"]} {result["verdict"]}: {OUT / result["run_id"]}.json')
    if args.check:
        exploration = result["exploration"]
        print(json.dumps({"checks": exploration["checks"], "outcome": exploration["outcome"],
                          "calls": len(exploration["attempts"]),
                          "input_tokens": exploration["reported_input_tokens"]}, separators=(",", ":")))
    return {"PASS": 0, "INCONCLUSIVE": 1, "FAIL": 2, "ERROR": 2, "CANCELLED": 1}[result["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
