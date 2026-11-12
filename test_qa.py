import json
import io
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

import qa
from qa_mock import MockGame, Rejected, make_server
from qa_project import STAGE, copy_project_inputs, install_plugin, plugin_preview, project_fingerprint, recover_project
from spikes import decisions_smoke


class MockQaTests(unittest.TestCase):
    def test_decisions_stops_after_wait_and_ignores_pose_only_drift(self):
        objective = "Verify that the character can move."
        session = {"session_id": "test", "schema_version": 1, "world_generation": 1,
                   "input_owner": "DECISIONS", "owner_revision": 1, "expected_mapping_revision": 1}
        for selected, checks in (("WAIT", ()), ("IA_Move", ()), ("IA_Move", ("IA_Move", "IA_Jump"))):
            position = 0

            def call(path, payload=None):
                nonlocal position
                if path == "/session":
                    return session
                if path == "/mappings":
                    return {"mapping_revision": 1, "keys": {"I": "IA_Move"}}
                if path == "/observations":
                    position += 1
                    return {**session, "observation_seq": 0, "on_ground": True, "player_exists": True,
                            "held_keys": [], "pawn_position": [position, 0, 0]}
                if path.startswith("/events"):
                    return {"lost": False, "events": [
                        {"layer": "L2", "kind": "Triggered", "value": "IA_Move"},
                    ]}
                return {}

            def decide(_prompt, _choices, *, metadata, **_options):
                metadata["input_tokens"] = 50
                return "OK", selected

            with (patch.object(qa, "ue_transport", return_value=nullcontext(call)),
                  patch.object(qa, "wait_for_ue_world"), patch.object(qa, "bridge_build_files", return_value={}),
                  patch.object(qa, "load_key", return_value="fixture"),
                  patch.object(qa, "request_choice", side_effect=decide) as model,
                  patch.object(qa, "run_command", return_value="completed") as execute,
                  patch.object(qa, "save_report", side_effect=lambda value: value),
                  patch.object(qa, "LIVE_INTERVAL_SECONDS", 0)):
                result = qa.explore(None, 5, 5, live=True, executable=Path(__file__), objective=objective,
                                    checks=checks)
            model.assert_called_once()
            prompt, choices = model.call_args.args
            self.assertIn(objective, prompt)
            self.assertEqual(set(choices), {"IA_Move", "WAIT"})
            self.assertEqual(result["exploration"]["objective"], objective)
            if selected == "WAIT":
                self.assertEqual(result["exploration"]["outcome"], "WAIT_NO_STATE_CHANGE")
                execute.assert_not_called()
            elif checks:
                self.assertEqual(result["verdict"], "INCONCLUSIVE")
                self.assertEqual(result["exploration"]["checks"], {"IA_Move": "PASS", "IA_Jump": "UNVERIFIED"})
                execute.assert_called_once()
            else:
                self.assertEqual(result["verdict"], "PASS")
                execute.assert_called_once()

    def test_decisions_rejects_an_extra_answer_and_out_of_range_options(self):
        valid = {"name": "next_action", "type": "choice", "choice": "WAIT"}
        extra = {"name": "unexpected", "type": "choice", "choice": "WAIT"}
        self.assertEqual(decisions_smoke.classify_answer({"answers": [valid, extra]}, {"WAIT"}),
                         ("DECISION_INVALID", None))
        with patch.object(decisions_smoke, "build_opener") as opener:
            for size in (1, 256):
                self.assertEqual(decisions_smoke.request_choice("fixture", {str(n): "" for n in range(size)}, key="dummy"),
                                 ("DECISION_INVALID_CANDIDATES", None))
            opener.assert_not_called()

    def test_decisions_candidates_follow_ue_bindings_and_pawn_state(self):
        mappings = {"keys": {"I": "IA_Move", "SpaceBar": "IA_Jump", "F": "WAIT",
                             "Q": "IA_Ambiguous", "E": "IA_Ambiguous"}}
        available = qa.discovered_actions(mappings, {"player_exists": True, "on_ground": True})
        self.assertEqual(set(available), {"IA_Move", "IA_Jump"})
        self.assertEqual(available["IA_Move"][0], ["I"])
        self.assertEqual(set(qa.discovered_actions(mappings, {"player_exists": True, "on_ground": False})),
                         {"IA_Move"})

    def test_actual_bridge_upgrade_recovers_after_backup_rename(self):
        import shutil

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "Sample.uproject"
            project.write_text("{}", encoding="utf-8")
            source = qa.ROOT / "plugins" / "GameQABridge"
            installed = install_plugin(project, source, plugin_preview(project, source)["approval"])
            target = Path(installed["target"])
            old_hashes = installed["file_hashes_after"]
            upgrade = root / "new-payload"
            shutil.copytree(source, upgrade)
            descriptor = upgrade / "GameQABridge.uplugin"
            metadata = json.loads(descriptor.read_text(encoding="utf-8"))
            metadata["Version"] += 1
            descriptor.write_text(json.dumps(metadata), encoding="utf-8")
            plan = plugin_preview(project, upgrade)
            child_code = """
import os, sys
from pathlib import Path
from qa_project import install_plugin
rename = Path.rename
def interrupt(self, destination):
    result = rename(self, destination)
    if destination.name == "backup":
        os._exit(73)
    return result
Path.rename = interrupt
install_plugin(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
"""
            child = subprocess.run([sys.executable, "-B", "-c", child_code, str(project), str(upgrade),
                                    plan["approval"]], timeout=15)
            self.assertEqual(child.returncode, 73)
            self.assertFalse(target.exists())
            self.assertEqual(recover_project(project), {"recovered": True})
            self.assertEqual(plugin_preview(project, source)["file_hashes_before"], old_hashes)
            self.assertEqual(install_plugin(project, upgrade, plan["approval"])["file_hashes_after"],
                             plan["file_hashes_after"])

    def test_ue_build_snapshot_keeps_sources_and_binary_only_plugins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "original"
            source.mkdir()
            project = source / "Sample.uproject"
            project.write_text("{}")
            for name in ("SourcePlugin/Source/Code.cpp", "SourcePlugin/Intermediate/compile.obj",
                         "SourcePlugin/SourcePlugin.uplugin", "BinaryPlugin/Binaries/Runtime.dll",
                         "BinaryPlugin/BinaryPlugin.uplugin"):
                path = source / "Plugins" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture")
            fingerprint = project_fingerprint(project, "5.9")
            copy = root / "copy"
            copy_project_inputs(project, copy)
            self.assertEqual(project_fingerprint(copy / project.name, "5.9"), fingerprint)
            self.assertFalse((copy / "Plugins/SourcePlugin/Intermediate").exists())
            self.assertTrue((copy / "Plugins/BinaryPlugin/Binaries/Runtime.dll").exists())

    def test_ue_node_preserves_repeats_gaps_and_combo_holds(self):
        mappings = {"keys": {"W": "IA_Move", "Up": "IA_Move", "SpaceBar": "IA_Jump"},
                    "catalog": {}, "mapping_revision": 3}
        steps = [
            {"kind": "wait", "duration_ms": 40},
            {"kind": "tap", "keys": ["Up"], "duration_ms": 80, "repeat": {"value": 2},
             "gap_ms": {"value": 30}},
            {"kind": "hold", "keys": ["Up", "SpaceBar"], "duration_ms": 100},
        ]
        plan = qa.preview_node(steps, mappings)
        self.assertEqual(qa.bridge_holds(plan, mappings), [
            {"start_ms": 40, "keys": ["Up"], "hold_ms": 80},
            {"start_ms": 150, "keys": ["Up"], "hold_ms": 80},
            {"start_ms": 230, "keys": ["Up", "SpaceBar"], "hold_ms": 100},
        ])
        overlap = qa.preview_node([
            {"kind": "down", "keys": ["Up"], "duration_ms": 50},
            {"kind": "hold", "keys": ["SpaceBar"], "duration_ms": 100},
            {"kind": "up", "keys": ["Up"]},
        ], mappings)
        with self.assertRaisesRegex(Rejected, "UE_OVERLAPPING_HOLDS_UNSUPPORTED"):
            qa.bridge_holds(overlap, mappings)

    def test_ue_submission_checks_status_before_resending(self):
        request_id = uuid4().hex
        route = f"/commands/{request_id}"
        for known in (True, False):
            calls = []

            def call(path, payload=None):
                calls.append((path, payload))
                if len(calls) == 1:
                    raise Rejected("UE_CONNECTION_ERROR")
                if len(calls) == 2 and not known:
                    raise Rejected("UNKNOWN_REQUEST")
                return {"status": "completed"}

            self.assertEqual(qa.run_command(call, {"request_id": request_id}, {"keys": ["W"]}), "completed")
            self.assertEqual([path for path, _ in calls],
                             ["/commands", route, route] if known else ["/commands", route, "/commands", route])
            if not known:
                self.assertEqual(calls[0][1], calls[2][1])

    def test_loopback_client_refuses_redirect_with_bearer_token(self):
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/unexpected")
                self.end_headers()

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                with self.assertRaisesRegex(Rejected, "LOOPBACK_REDIRECT"):
                    qa.loopback_client(server.server_port, "fixture-token")("/health")
                self.assertEqual(paths, ["/qa/v1/health"])
            finally:
                server.shutdown()
                worker.join()

    def test_scripted_scenario_assertions_and_replay(self):
        scenario = {"schema_version": "1.0", "id": "movement", "mode": "scripted",
                    "preconditions": ["game_ready", "player_exists", {"input_action_available": "JUMP"}],
                    "actions": [
                        {"id": "move", "do": "MOVE_FORWARD", "hold_ms": 20,
                         "assertions": [{"type": "position_delta_min", "value": 20}]},
                        {"id": "jump", "do": "JUMP", "press_ms": 20,
                         "assertions": [{"type": "input_action_event", "action": "JUMP"}]},
                    ]}
        with tempfile.TemporaryDirectory() as directory, patch.object(qa, "OUT", Path(directory)):
            result = qa.run_scenario(scenario)
            self.assertEqual(result["verdict"], "PASS")
            self.assertEqual(result["after"]["position"], 20)
            self.assertEqual(result["after"]["jumps"], 1)
            replay = qa.replay(Path(directory) / (result["run_id"] + ".json"))
            self.assertTrue(replay["comparison"]["matches"])
            scenario["actions"][0]["assertions"][0]["value"] = 21
            failed = qa.run_scenario(scenario)
            self.assertEqual(failed["verdict"], "FAIL")
            self.assertEqual(failed["after"]["jumps"], 0)

    def test_input_owner_handoff_cancels_and_fences_previous_owner(self):
        with qa.transport() as call:
            session = call("/session", {"schema_version": 1, "input_owner": "DECISIONS"})
            envelope = {**session, "request_id": uuid4().hex, "deadline_ms": 2000}
            call("/commands", {**envelope, "keys": ["W"], "hold_ms": 1000})
            self.assertEqual(call("/observations")["held_keys"], ["W"])
            manual = call("/session/owner", {**envelope, "request_id": uuid4().hex, "next_owner": "MANUAL"})
            self.assertEqual(call("/observations")["held_keys"], [])
            self.assertEqual(call(f"/commands/{envelope['request_id']}")["status"], "cancelled")
            for route, values in (
                    ("/commands", {"keys": ["W"], "hold_ms": 20}),
                    ("/input/release-all", {}), ("/session/heartbeat", {})):
                with self.assertRaisesRegex(Rejected, "INPUT_OWNER_MISMATCH"):
                    call(route, {**envelope, **values, "request_id": uuid4().hex})
            call("/session/owner", {**manual, "request_id": uuid4().hex,
                                    "deadline_ms": 100, "next_owner": "DECISIONS"})
            with self.assertRaisesRegex(Rejected, "STALE_INPUT_OWNER"):
                call("/commands", {**envelope, "request_id": uuid4().hex, "keys": ["W"], "hold_ms": 20})

    def test_catalog_restricts_exact_keys_and_unconfirmed_actions(self):
        profile = {"catalog_version": "1.0", "actions": [
            {"id": "LEAP", "input_actions": ["IA_Jump"], "description": "Jump when grounded",
             "semantic_status": "USER_CONFIRMED", "ai_allowed": True, "prerequisites": ["on_ground"]},
            {"id": "RUN", "input_actions": ["IA_Move"], "description": "Move",
             "semantic_status": "NEEDS_CONFIRMATION", "ai_allowed": True},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(profile), encoding="utf-8")
            catalog = qa.read_catalog(path)
        mappings = {"keys": {"SpaceBar": "IA_Jump", "J": "IA_Jump", "W": "IA_Move"}}
        with self.assertRaisesRegex(Rejected, "NO_VALID_ACTION"):
            qa.action_candidates(None, mappings, catalog, "all_mapped", None, {"on_ground": True})
        result = qa.action_candidates(None, mappings, catalog, "selected_keys", ["J", "W"], {"on_ground": True})
        self.assertEqual(result, {"LEAP": (["J"], "IA_Jump", "Jump when grounded")})
        with self.assertRaisesRegex(Rejected, "NO_VALID_ACTION"):
            qa.action_candidates(None, mappings, catalog, "selected_keys", ["J"], {"on_ground": False})

    def test_missing_observation_never_passes(self):
        triggered = [{"kind": "Triggered", "value": "IA_Jump"}]
        self.assertEqual(qa.evaluate("completed", "IA_Jump", {"jumps": 0}, {"held_keys": []}, triggered),
                         "INCONCLUSIVE")
        self.assertEqual(qa.evaluate("completed", "IA_Jump", {"jumps": 0},
                                     {"held_keys": [], "jumps": 1}, None), "INCONCLUSIVE")

    def test_retry_after_and_usage_are_sanitized(self):
        headers = Message()
        headers["Retry-After"] = "4"
        error = HTTPError("https://api.openai.com/v1/decisions", 429, "limit", headers,
                          io.BytesIO(b'{"error":{"code":"insufficient_quota","message":"discard"}}'))
        with patch.object(decisions_smoke, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            metadata = {}
            result = decisions_smoke.request_choice("synthetic", {"WAIT": "wait", "MOVE": "move"}, key="dummy",
                                                    metadata=metadata)
            self.assertEqual(result, ("API_RATE_OR_QUOTA", None))
            self.assertEqual(metadata, {"quota_exhausted": True, "retry_after_seconds": 4})

    def test_exploration_retries_and_budget_guards(self):
        responses = iter([
            ("API_RATE_OR_QUOTA", None, {"retry_after_seconds": 0.01}),
            ("API_TRANSIENT_ERROR", None, {}),
            ("OK", "JUMP", {"input_tokens": 60, "total_tokens": 60}),
        ])

        def fake_choice(_state, _choices, *, metadata, **_kwargs):
            status, choice, fields = next(responses)
            metadata.update(fields)
            return status, choice

        with (tempfile.TemporaryDirectory() as directory, patch.object(qa, "OUT", Path(directory)),
              patch.object(qa.os, "environ", {}), patch.object(qa, "load_key", return_value="dummy") as load,
              patch.object(qa, "request_choice", side_effect=fake_choice) as choice,
              patch.object(qa, "LIVE_INTERVAL_SECONDS", 0.01)):
            blocked = qa.explore(["JUMP"], 5, 3, live=True, max_input_tokens=100)
            self.assertEqual(blocked["exploration"]["attempts"], [])
            load.assert_not_called()
            choice.assert_not_called()
            result = qa.explore(["JUMP"], 5, 3, live=True, max_input_tokens=3100)
            self.assertEqual(choice.call_count, 3)
            self.assertEqual(result["after"]["jumps"], 1)
            self.assertEqual(result["exploration"]["reported_input_tokens"], 60)
            self.assertEqual(result["exploration"]["outcome"], "BUDGET_EXHAUSTED")
            choice.reset_mock(side_effect=True)
            choice.return_value = ("API_TIMEOUT", None)
            timed_out = qa.explore(["JUMP"], 5, 3, live=True)
            choice.assert_called_once()
            self.assertEqual(timed_out["exploration"]["outcome"], "API_TIMEOUT")
            self.assertEqual(timed_out["after"]["held_keys"], [])
            choice.reset_mock()
            def quota(_state, _choices, *, metadata, **_kwargs):
                metadata.update(quota_exhausted=True, retry_after_seconds=1)
                return "API_RATE_OR_QUOTA", None
            choice.side_effect = quota
            denied = qa.explore(["JUMP"], 5, 3, live=True)
            choice.assert_called_once()
            self.assertEqual(denied["exploration"]["outcome"], "API_RATE_OR_QUOTA")

    def test_exploration_drops_choice_after_remap(self):
        game = MockGame()
        transport = qa.transport

        def decision(_state, _choices, *, metadata, **_kwargs):
            game.update_mappings({"J": "IA_Jump"})
            metadata["input_tokens"] = 60
            return "OK", "JUMP"

        with (tempfile.TemporaryDirectory() as directory, patch.object(qa, "OUT", Path(directory)),
              patch.object(qa, "transport", side_effect=lambda: transport(game)),
              patch.object(qa.os, "environ", {}), patch.object(qa, "load_key", return_value="dummy"),
              patch.object(qa, "request_choice", side_effect=decision)):
            report = qa.explore(["JUMP"], 2, 3, live=True)
        self.assertEqual(report["exploration"]["outcome"], "STALE_DECISION")
        self.assertEqual(report["after"]["held_keys"], [])
        self.assertEqual(report["events"], [])

    def test_interrupted_plugin_stage_can_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "Sample.uproject"
            project.write_text("{}", encoding="utf-8")
            source = root / "payload"
            source.mkdir()
            (source / "GameQABridge.uplugin").write_text("{}", encoding="utf-8")
            (source / "fixture.txt").write_text("mock", encoding="utf-8")
            plan = plugin_preview(project, source)
            original = project_fingerprint(project, "mock")
            child_code = """
import sys, time
from pathlib import Path
from qa_project import install_plugin
checks = iter((False, True))
def cancelled():
    if next(checks):
        print("STAGED", flush=True)
        time.sleep(20)
    return False
install_plugin(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], cancelled)
"""
            with subprocess.Popen([sys.executable, "-B", "-c", child_code, str(project), str(source),
                                   plan["approval"]], stdout=subprocess.PIPE, text=True) as child:
                try:
                    self.assertEqual(child.stdout.readline().strip(), "STAGED")
                finally:
                    child.kill()
                    child.wait(timeout=3)
            self.assertFalse(Path(plan["target"]).exists())
            self.assertEqual(project_fingerprint(project, "mock"), original)
            self.assertTrue((root / STAGE).exists())
            self.assertEqual(recover_project(project), {"recovered": True})
            self.assertFalse((root / STAGE).exists())
            install_plugin(project, source, plan["approval"])
            self.assertEqual((Path(plan["target"]) / "fixture.txt").read_text(), "mock")

    def test_plugin_cancel_conflict_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "Sample.uproject"
            project.write_text("{}", encoding="utf-8")
            plugin = root / "fixture"
            plugin.mkdir()
            descriptor = plugin / "GameQABridge.uplugin"
            descriptor.write_text('{"Version":1}', encoding="utf-8")
            before = project_fingerprint(project, "mock")
            preview = plugin_preview(project, plugin)
            target = Path(preview["target"])
            with self.assertRaisesRegex(Rejected, "PLUGIN_INSTALL_CANCELLED"):
                install_plugin(project, plugin, preview["approval"], cancelled=lambda: True)
            self.assertFalse(target.exists())
            self.assertEqual(project_fingerprint(project, "mock"), before)
            install_plugin(project, plugin, preview["approval"])
            second = plugin_preview(project, plugin)
            self.assertEqual(second["approved_operations"], [])
            self.assertEqual(install_plugin(project, plugin, second["approval"]), second)
            self.assertNotEqual(project_fingerprint(project, "mock"), before)
            installed = target / descriptor.name
            installed.write_text('{"Version":2}', encoding="utf-8")
            with self.assertRaisesRegex(Rejected, "PLUGIN_DESTINATION_CONFLICT"):
                plugin_preview(project, plugin)
            self.assertEqual(installed.read_text(encoding="utf-8"), '{"Version":2}')
            self.assertEqual(project.read_text(encoding="utf-8"), "{}")

    def test_upgrade_and_remove_preserve_project_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "Game.uproject"
            project.write_text("{}", encoding="utf-8")
            source = root / "fixture"
            source.mkdir()
            descriptor = source / "GameQABridge.uplugin"
            descriptor.write_text('{"Version":1}', encoding="utf-8")
            original = plugin_preview(project, source)
            install_plugin(project, source, original["approval"])
            descriptor.write_text('{"Version":2}', encoding="utf-8")
            upgrade = plugin_preview(project, source)
            self.assertEqual(upgrade["approved_operations"], ["upgrade_plugin"])
            with self.assertRaisesRegex(Rejected, "PLUGIN_INSTALL_CANCELLED"):
                install_plugin(project, source, upgrade["approval"], cancelled=lambda: True)
            target = Path(upgrade["target"])
            self.assertEqual((target / descriptor.name).read_text(), '{"Version":1}')
            stage = root / STAGE
            stage.mkdir()
            (stage / "journal.json").write_text(json.dumps({**upgrade, "installation_id": uuid4().hex}),
                                                encoding="utf-8")
            target.rename(stage / "backup")
            self.assertEqual(recover_project(project), {"recovered": True})
            self.assertEqual((target / descriptor.name).read_text(), '{"Version":1}')
            install_plugin(project, source, upgrade["approval"])
            self.assertEqual((target / descriptor.name).read_text(), '{"Version":2}')
            remove = plugin_preview(project)
            self.assertEqual(remove["approved_operations"], ["remove_plugin"])
            install_plugin(project, None, remove["approval"])
            self.assertFalse(target.exists())
            self.assertEqual(project.read_text(), "{}")

    def test_1000_commands_and_mapping_revision(self):
        game = MockGame()
        with qa.transport(game) as call:
            session = call("/session", {"schema_version": 1})
            game.update_mappings({"J": "IA_Jump"})
            command = {**session, "request_id": uuid4().hex, "deadline_ms": 100,
                       "steps": [{"kind": "down", "logical_action": "JUMP"}]}
            with self.assertRaisesRegex(Rejected, "MAPPING_CHANGED"):
                call("/commands", command)
            command["expected_mapping_revision"] = call("/mappings")["mapping_revision"]
            with self.assertRaisesRegex(Rejected, "STALE_OBSERVATION"):
                call("/commands", {**command, "expected_observation_seq": 1})
            for _ in range(1000):
                command["request_id"] = uuid4().hex
                self.assertEqual(call("/commands", command)["status"], "completed")
            self.assertEqual(call("/commands", command)["status"], "completed")
            self.assertEqual(call("/observations")["jumps"], 1000)
            self.assertEqual(call("/observations")["held_keys"], [])

    def test_controller_process_death_releases_input(self):
        server, token = make_server()
        with server:
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
            worker.start()
            child_code = """
import json, sys, time
from qa import loopback_client
port, token, session = json.loads(sys.stdin.readline())
call = loopback_client(port, token)
from uuid import uuid4
body = {**session, "request_id": uuid4().hex, "deadline_ms": 2000}
call("/commands", {**body, "keys": ["W"], "hold_ms": 2000})
print(body["request_id"], flush=True)
time.sleep(5)
"""
            try:
                opener = build_opener(ProxyHandler({}))
                request = Request(f"http://127.0.0.1:{server.server_port}/qa/v1/session",
                                  data=b'{"schema_version":1}', headers={"Authorization": f"Bearer {token}",
                                                                         "Content-Type": "application/json"})
                with opener.open(request, timeout=3) as response:
                    session = json.load(response)
                session_token = session.pop("session_token")
                self.assertNotEqual(session_token, token)
                with self.assertRaisesRegex(Rejected, "UNAUTHORIZED"):
                    qa.loopback_client(server.server_port, token)("/observations")
                with subprocess.Popen(
                    [sys.executable, "-B", "-c", child_code],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                    cwd=Path(qa.__file__).parent,
                ) as child:
                    child.stdin.write(json.dumps([server.server_port, session_token, session]) + "\n")
                    child.stdin.flush()
                    request = child.stdout.readline().strip()
                    try:
                        call = qa.loopback_client(server.server_port, session_token)
                        self.assertEqual(call("/observations")["held_keys"], ["W"])
                    finally:
                        child.kill()
                        child.wait(timeout=3)
                deadline = time.monotonic() + 1.5
                while call(f"/commands/{request}")["status"] == "running" and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(call(f"/commands/{request}")["status"], "expired")
                self.assertEqual(call("/observations")["held_keys"], [])
                self.assertTrue(any(e["kind"] == "LeaseExpired" for e in call("/events")["events"]))
                new_session = qa.loopback_client(server.server_port, token)("/session", {"schema_version": 1})
                self.assertNotEqual(new_session["session_id"], session["session_id"])
                self.assertNotIn("session_token", new_session)
                with self.assertRaisesRegex(Rejected, "UNAUTHORIZED"):
                    call("/observations")
            finally:
                server.shutdown()
                worker.join()

    def test_exploration_uses_one_session_and_call_budget(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(qa, "OUT", Path(directory)):
            result = qa.explore(["MOVE", "JUMP"], max_calls=2, max_seconds=5)
            attempts = result["exploration"]["attempts"]
            self.assertEqual([a["choice"] for a in attempts], ["MOVE", "JUMP"])
            self.assertEqual(attempts[1]["before"], attempts[0]["after"])
            self.assertEqual(result["after"]["held_keys"], [])
            self.assertEqual(result["exploration"]["outcome"], "BUDGET_EXHAUSTED")
            path = Path(directory) / (result["run_id"] + ".json")
            with patch.object(qa, "request_choice", side_effect=AssertionError("Unexpected API call")):
                replay = qa.replay(path)
            self.assertTrue(replay["comparison"]["matches"])

    def test_node_timing_and_runtime_remap(self):
        steps = [
            {"kind": "down", "logical_action": "MOVE_FORWARD"},
            {"kind": "key_tap", "logical_action": "JUMP", "repeat": {"default": 2},
             "gap_ms": {"default": 10},
             "duration_ms": {"min": 20, "default": 30, "max": 45, "value": 40}},
            {"kind": "up", "logical_action": "MOVE_FORWARD"},
            {"kind": "hold", "logical_action": "MOVE_FORWARD", "duration_ms": 20},
        ]
        with (tempfile.TemporaryDirectory() as directory, patch.object(qa, "OUT", Path(directory)),
              patch.dict(qa.MAPPINGS, {"Up": "IA_Move", "J": "IA_Jump"}, clear=True)):
            result = qa.execute_input({"steps": steps}, "IA_Jump")
            self.assertEqual(result["verdict"], "PASS")
            self.assertEqual(result["after"]["jumps"], 2)
            self.assertEqual(result["after"]["position"], 110)
            self.assertEqual(result["after"]["held_keys"], [])
            self.assertEqual(result["input_plan"]["timeline"][:4], [
                [0, "down", "Up"], [0, "down", "J"], [40, "up", "J"], [50, "down", "J"],
            ])
            path = Path(directory) / (result["run_id"] + ".json")
            self.assertEqual(qa.replay(path)["input_plan"], result["input_plan"])
            self.assertEqual(qa.decide(["JUMP"])["step"]["keys"], ["J"])
            steps[1]["duration_ms"]["value"] = 46
            with self.assertRaisesRegex(Rejected, "INVALID_NUMBER_RANGE"):
                qa.execute_input({"steps": steps}, "IA_Jump")

    def test_panel_opens_bound_browser_address(self):
        opened = []
        finished = threading.Event()
        with qa.make_panel_server(0) as server:
            def open_browser(url):
                try:
                    opened.append(url)
                    opener = build_opener(ProxyHandler({}))
                    with opener.open(url, timeout=3) as response:
                        opened.append(response.status)
                finally:
                    finished.set()

            with (patch.object(qa, "make_panel_server", return_value=server),
                  patch.object(server, "serve_forever", side_effect=server.handle_request),
                  patch.object(qa.webbrowser, "open", side_effect=open_browser)):
                qa.panel(0, open_browser=True)
                self.assertTrue(finished.wait(3))
            self.assertEqual(opened, [f"http://127.0.0.1:{server.server_port}", 200])

    def test_registration_stays_local(self):
        with (patch.object(qa, "getpass", return_value=" test-secret "),
              patch.object(qa, "save_key") as save,
              patch.object(decisions_smoke, "build_opener") as opener):
            qa.register_key()
            save.assert_called_once_with("test-secret")
            opener.assert_not_called()

    def test_live_key_is_only_in_auth_header(self):
        with patch.object(decisions_smoke, "build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value = b'{"answers":[{"name":"next_action","type":"choice","choice":"JUMP"}]}'
            self.assertEqual(decisions_smoke.check_connection(key="test-secret"), ("OK", "JUMP"))
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
            self.assertNotIn("test-secret", request.full_url)
            self.assertNotIn(b"test-secret", request.data)

    def test_decisions_network_error_codes(self):
        for error, expected in (
            (socket.gaierror(), "API_DNS_ERROR"),
            (ssl.SSLError(), "API_TLS_ERROR"),
            (TimeoutError(), "API_TIMEOUT"),
            (ConnectionRefusedError(), "API_NETWORK_ERROR"),
        ):
            with self.subTest(expected=expected), patch.object(decisions_smoke, "build_opener") as opener:
                opener.return_value.open.side_effect = URLError(error)
                self.assertEqual(decisions_smoke.check_connection(key="dummy"), (expected, None))

    def test_panel_over_http(self):
        with (tempfile.TemporaryDirectory() as directory, patch.object(qa, "OUT", Path(directory)),
              patch.object(qa, "explore", return_value={"verdict": "PASS"}) as explore,
              qa.make_panel_server(0, Path("Fixture.exe")) as server):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                opener = build_opener(ProxyHandler({}))
                with opener.open(url, timeout=3) as response:
                    self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                    page = response.read().decode()
                token = re.search(r'name="qa-token" content="([^"]+)"', page)[1]
                payload = {"max_calls": 1}

                def send(path, origin, body=payload):
                    request = Request(url + path, data=json.dumps(body).encode(), headers={
                        "Origin": origin, "X-QA-Token": token, "Content-Type": "application/json",
                    })
                    with opener.open(request, timeout=3) as response:
                        return json.load(response)

                with self.assertRaises(HTTPError) as error:
                    send("/run", "https://example.invalid")
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
                self.assertEqual(send("/run", url)["verdict"], "PASS")
                self.assertEqual(explore.call_args.args, (None, 1, 60))
                self.assertTrue(explore.call_args.kwargs["live"])
                with self.assertRaises(HTTPError) as error:
                    send("/run", url, {"keys": ["SpaceBar"], "hold_ms": 30})
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
                explore.assert_called_once()
                self.assertNotIn('id="test-form"', page)
                self.assertNotIn('type="password"', page)
                with self.assertRaises(HTTPError) as error:
                    send("/settings/api-key", url, {"key": "test-secret"})
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
            finally:
                server.shutdown()
                thread.join()

    def test_manual_report_and_replay(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(qa, "OUT", Path(directory)):
            result = qa.execute(["W", "LeftShift"], 30, "IA_Move")
            self.assertEqual(result["verdict"], "PASS")
            self.assertEqual(result["after"]["position"], 60)
            self.assertEqual(result["after"]["held_keys"], [])
            self.assertEqual({e["layer"] for e in result["events"]}, {"L1", "L2", "L3"})
            path = Path(directory) / (result["run_id"] + ".json")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), result)
            self.assertTrue(path.with_suffix(".html").is_file())
            with patch.object(qa, "request_choice", side_effect=AssertionError("Unexpected API call")):
                replay = qa.replay(path)
            self.assertEqual(replay["replay_of"], result["run_id"])
            self.assertEqual(replay["after"]["position"], result["after"]["position"])

    def test_oracles_and_cancel(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(qa, "OUT", Path(directory)):
            self.assertEqual(qa.execute(["SpaceBar"], 20, "IA_Move")["verdict"], "FAIL")
            self.assertEqual(qa.execute(["SpaceBar"], 20, "")["verdict"], "INCONCLUSIVE")
            cancel = threading.Event()
            cancel.set()
            result = qa.execute(["W"], 1000, "IA_Move", cancel=cancel)
            self.assertEqual(result["command_status"], "cancelled")
            self.assertEqual(result["after"]["held_keys"], [])
            self.assertEqual(result["after"]["position"], 0)

    def test_decision_scope_and_error(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(qa, "OUT", Path(directory)):
            result = qa.decide(["JUMP"])
            self.assertEqual(result["decision"]["candidates"], ["JUMP", "WAIT"])
            self.assertEqual(result["step"]["keys"], ["SpaceBar"])
            self.assertEqual(result["after"]["jumps"], 1)
            with (patch.object(qa, "load_key", return_value="test-secret"),
                  patch.dict(qa.os.environ, {"OPENAI_API_KEY": ""}),
                  patch.object(qa, "request_choice", return_value=("DECISION_INVALID", None)) as request):
                failed = qa.decide(["JUMP"], live=True)
            self.assertEqual(request.call_args.kwargs["key"], "test-secret")
            self.assertEqual(failed["verdict"], "ERROR")
            self.assertEqual(failed["events"], [])

    def test_http_guards_and_release(self):
        with qa.transport() as call:
            session = call("/session", {"schema_version": 1})
            body = {**session, "request_id": uuid4().hex, "deadline_ms": 2000,
                    "keys": ["W"], "hold_ms": 1000}
            with self.assertRaisesRegex(Rejected, "STALE_WORLD"):
                call("/commands", {**body, "world_generation": -1})
            with self.assertRaisesRegex(Rejected, "UNMAPPED_OR_DUPLICATE_KEY"):
                call("/commands", {**body, "keys": ["Escape"]})
            self.assertEqual(call("/commands", body), call("/commands", body))
            with self.assertRaisesRegex(Rejected, "IDEMPOTENCY_CONFLICT"):
                call("/commands", {**body, "hold_ms": 900})
            call("/input/release-all", {**body, "request_id": uuid4().hex})
            events = call("/events?after_seq=0")["events"]
            self.assertEqual(sum(e["kind"] == "KeyDown" for e in events), 1)
            self.assertEqual(sum(e["kind"] == "KeyUp" for e in events), 1)
            self.assertEqual(call("/observations")["held_keys"], [])


if __name__ == "__main__":
    unittest.main()
