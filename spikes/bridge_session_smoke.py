"""Verify the session contract against a packaged Win64 QA game."""

import argparse
import http.client
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from qa import loopback_client, run_command, ue_transport
from qa_mock import Rejected


def expect_rejection(call, route, body, code):
    try:
        call(route, body)
    except Rejected as error:
        if str(error) == code:
            return
        raise AssertionError(f"{route} returned {error}, expected {code}") from error
    raise AssertionError(f"{route} accepted a request expected to return {code}")


def envelope(context, **changes):
    return {**context, "request_id": uuid4().hex, "deadline_ms": 500, **changes}

def check_controller_death(executable):
    address = []

    def capture_client(port, token, **options):
        address.extend((port, token))
        return loopback_client(port, token, **options)

    with patch("qa.loopback_client", capture_client), ue_transport(executable) as bootstrap:
        deadline = time.monotonic() + 15
        while not bootstrap("/health")["ready"]:
            assert time.monotonic() < deadline, "Game not ready"
            time.sleep(0.05)
        port, token = address
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            conn.request("POST", "/qa/v1/session", body='{"schema_version":1}', headers={
                "Authorization": f"Bearer {token}", "Content-Type": "application/json",
            })
            response = conn.getresponse()
            assert response.status == 200
            context = json.load(response)
        finally:
            conn.close()
        session_token = context.pop("session_token")
        observe = loopback_client(port, session_token)
        payload = envelope(context, keys=["W"], hold_ms=2000, deadline_ms=3000)
        child_code = """
import json, os, time
from qa import loopback_client
call = loopback_client(int(os.environ["QA_TEST_PORT"]), os.environ["QA_TEST_TOKEN"])
call("/commands", json.loads(os.environ["QA_TEST_COMMAND"]))
time.sleep(10)
"""
        child_env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")
                     if key in os.environ}
        child_env.update(QA_TEST_PORT=str(port), QA_TEST_TOKEN=session_token, QA_TEST_COMMAND=json.dumps(payload))
        with subprocess.Popen([sys.executable, "-B", "-c", child_code], env=child_env,
                              creationflags=subprocess.CREATE_NO_WINDOW) as controller:
            try:
                deadline = time.monotonic() + 2
                while observe("/observations")["observed_pressed_keys"] != ["W"]:
                    assert controller.poll() is None and time.monotonic() < deadline, "W did not go down"
                    time.sleep(0.01)
            finally:
                controller.kill()
            stopped = time.monotonic()
        while True:
            state = observe("/observations")
            if state["lease_lost"] and state["held_keys"] == state["observed_pressed_keys"] == []:
                break
            assert time.monotonic() - stopped < 2, "W remained pressed after controller death"
            time.sleep(0.01)
        released_ms = (time.monotonic() - stopped) * 1000
        assert observe("/commands/" + payload["request_id"]) == {"status": "cancelled"}
        replacement = bootstrap("/session", {"schema_version": 1})
        expect_rejection(observe, "/observations", None, "UNAUTHORIZED")
        assert bootstrap("/session/stop", envelope(replacement)) == {"held_keys": []}
        print(f"PASS: forced controller death released W in {released_ms:.0f} ms and fenced the old token")



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--commands", type=int, choices=(0, 1000), default=0)
    parser.add_argument("--controller-death", action="store_true")
    args = parser.parse_args()
    with ue_transport(args.executable) as call:
        deadline = time.monotonic() + 15
        while not call("/health")["ready"]:
            if time.monotonic() >= deadline:
                raise AssertionError("The packaged game did not become ready")
            time.sleep(0.05)
        start = {"schema_version": 1, "input_owner": "MANUAL"}
        expect_rejection(call, "/session", {**start, "oversized": "x" * 16384}, "BODY_TOO_LARGE")
        expect_rejection(call, "/session", {"schema_version": "1"}, "SCHEMA_MISMATCH")
        first = call("/session", start)
        expect_rejection(call, "/session", start, "SESSION_BUSY")
        expect_rejection(call, "/session/heartbeat", envelope(first, world_generation=-1), "STALE_WORLD")
        expect_rejection(call, "/session/heartbeat", envelope(first, expected_mapping_revision=-1), "MAPPING_CHANGED")
        expect_rejection(call, "/session/heartbeat", envelope(first, owner_revision=-1), "STALE_INPUT_OWNER")
        assert call("/session/heartbeat", envelope(first)) == {"lease_ms": 750}
        scripted = call("/session/owner", envelope(first, next_owner="SCRIPTED"))
        assert scripted["input_owner"] == "SCRIPTED" and scripted["owner_revision"] == 2
        expect_rejection(call, "/session/heartbeat", envelope(first), "INPUT_OWNER_MISMATCH")
        time.sleep(0.85)
        expect_rejection(call, "/session/heartbeat", envelope(scripted), "LEASE_EXPIRED")
        replacement = call("/session", start)
        assert replacement["session_id"] != first["session_id"]
        expect_rejection(call, "/session/heartbeat", envelope(scripted), "SESSION_MISMATCH")
        before = call("/observations")
        assert call("/mappings")["keys"] == {"W": "IA_Move", "SpaceBar": "IA_Jump"}
        command = envelope(replacement, deadline_ms=1000)
        combo = {"keys": ["W", "SpaceBar"], "hold_ms": 150}
        assert run_command(call, command, combo) == "completed"
        after = call("/observations")
        assert after["pawn_position"][0] > before["pawn_position"][0]
        assert after["pawn_position"][2] > before["pawn_position"][2]
        assert after["held_keys"] == []
        assert call("/commands", {**command, **combo}) == {"status": "completed"}
        expect_rejection(call, "/commands", {**command, **combo, "hold_ms": 155}, "IDEMPOTENCY_CONFLICT")

        hold = envelope(replacement, deadline_ms=3000)
        call("/commands", {**hold, "keys": ["W"], "hold_ms": 2000})
        deadline = time.monotonic() + 0.5
        while call("/observations")["held_keys"] != ["W"]:
            if time.monotonic() >= deadline:
                raise AssertionError("W was never held")
            time.sleep(0.02)
        time.sleep(0.85)
        assert call("/commands/" + hold["request_id"]) == {"status": "cancelled"}
        observed = call("/observations")
        assert observed["held_keys"] == observed["observed_pressed_keys"] == []
        assert call("/session/stop", envelope(replacement)) == {"held_keys": []}
        expect_rejection(call, "/session/heartbeat", envelope(replacement), "NO_SESSION")
        if args.commands:
            context = call("/session", start)
            cursor = 0
            key_edges = 0
            press = {"keys": ["SpaceBar"], "hold_ms": 20}
            for _ in range(args.commands):
                command = envelope(context, deadline_ms=1000, expected_observation_seq=cursor)
                assert run_command(call, command, press) == "completed"
                assert call("/commands", {**command, **press}) == {"status": "completed"}
                changes = call(f"/events?after_seq={cursor}")
                assert not changes["lost"]
                inputs = [edge for edge in changes["events"] if edge["layer"] == "L1"]
                assert [(edge["request_id"], edge["kind"], edge["value"]) for edge in inputs] == [
                    (command["request_id"], "KeyDown", "SpaceBar"),
                    (command["request_id"], "KeyUp", "SpaceBar"),
                ]
                key_edges += len(inputs)
                cursor = changes["events"][-1]["seq"]
            assert key_edges == 2 * args.commands
            assert call("/observations")["held_keys"] == []
            assert call("/session/stop", envelope(context)) == {"held_keys": []}
            print(f"PASS: {args.commands} commands and {args.commands} duplicate submissions produced {key_edges} key edges")
    print("PASS: sessions, limits, context fences, owner handoff, actual move/jump, duplicate and lease release")
    if args.controller_death:
        check_controller_death(args.executable)


if __name__ == "__main__":
    main()
