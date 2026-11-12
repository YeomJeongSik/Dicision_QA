"""Verify overlap, landing and a 1.1-second Game Thread stall in the recovery fixture."""

import argparse
import time
from pathlib import Path

from qa import bridge_timeline, preview_node, run_command, ue_input_verdict, ue_transport, wait_for_ue_world
from spikes.bridge_session_smoke import envelope, expect_rejection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    with ue_transport(args.executable) as call:
        wait_for_ue_world(call)
        armed_at = time.monotonic() + 7
        capabilities = call("/capabilities")
        assert capabilities["timeline"] and capabilities["jump_rise_event"]
        mappings = {**call("/mappings"), "catalog": {}}
        session = call("/session", {"schema_version": 1})
        before = call("/observations")
        plan = preview_node([
            {"kind": "down", "keys": ["W"], "duration_ms": 100},
            {"kind": "down", "keys": ["SpaceBar"], "duration_ms": 140},
            {"kind": "up", "keys": ["W"], "duration_ms": 80},
            {"kind": "up", "keys": ["SpaceBar"], "duration_ms": 1380},
        ], mappings)
        inputs = bridge_timeline(plan)
        expect_rejection(call, "/commands", envelope(session, deadline_ms=2500, duration_ms=100,
                         timeline=[[0, "down", "W"], [40, "down", "W"]]), "INVALID_INPUT")
        status = run_command(call, envelope(session, deadline_ms=2500), inputs)
        after = call("/observations")
        events = call(f"/events?after_seq={before['observation_seq']}")
        assert [(event["kind"], event["value"]) for event in events["events"] if event["layer"] == "L1"] == [
            ("KeyDown", "W"), ("KeyDown", "SpaceBar"), ("KeyUp", "W"), ("KeyUp", "SpaceBar"),
        ]
        assert after["on_ground"] and abs(after["pawn_position"][2] - before["pawn_position"][2]) < 0.5
        assert ue_input_verdict(status, ["IA_Move", "IA_Jump"], before, after, events, capabilities) == "PASS"
        print("PASS: overlapping keys retained their edge order; landed jump passed from in-flight evidence")

        while time.monotonic() < armed_at:
            call("/session/heartbeat", envelope(session))
            time.sleep(0.15)
        cursor = call("/observations")["observation_seq"]
        started = time.monotonic()
        status = run_command(call, envelope(session, deadline_ms=3000), {"keys": ["W"], "hold_ms": 1800})
        elapsed = time.monotonic() - started
        assert status == "cancelled" and 1.0 <= elapsed < 1.8, (status, elapsed)
        deadline = time.monotonic() + 0.3
        while True:
            state = call("/observations")
            if state["observed_pressed_keys"] == []:
                break
            assert time.monotonic() < deadline, state
            time.sleep(0.01)
        assert not state["lease_lost"] and state["held_keys"] == []
        released = call(f"/events?after_seq={cursor}")["events"]
        assert [(event["kind"], event["value"]) for event in released if event["layer"] == "L1"] == [
            ("KeyDown", "W"), ("KeyUp", "W"),
        ]
        call("/session/stop", envelope(session))
        print(f"PASS: {elapsed:.3f}s stalled command cancelled and W released while lease remained valid")


if __name__ == "__main__":
    main()
