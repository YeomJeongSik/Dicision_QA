"""Verify the generated Third Person fixture's timed W-to-I remap."""

import argparse
import time
from pathlib import Path

from qa import run_command, ue_transport, wait_for_ue_world
from qa_mock import Rejected
from spikes.bridge_session_smoke import envelope, expect_rejection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    with ue_transport(args.executable) as call:
        wait_for_ue_world(call)
        initial = call("/mappings")
        assert initial["keys"]["W"] == "IA_Move" and "I" not in initial["keys"]
        assert initial["active_contexts"] and initial["effective_mappings"]
        session = call("/session", {"schema_version": 1, "input_owner": "MANUAL"})
        deadline = time.monotonic() + 20
        while True:
            current = call("/mappings")
            if current["keys"].get("I") == "IA_Move":
                break
            revision = call("/observations")["expected_mapping_revision"]
            try:
                call("/session/heartbeat", envelope(session, expected_mapping_revision=revision))
            except Rejected as error:
                if str(error) != "MAPPING_CHANGED":
                    raise
            assert time.monotonic() < deadline, "Timed remap did not become active"
            time.sleep(0.1)
        assert current["mapping_revision"] > initial["mapping_revision"] and "W" not in current["keys"]
        expect_rejection(call, "/commands", envelope(session, keys=["W"], hold_ms=100), "MAPPING_CHANGED")
        refreshed = {**session, "expected_mapping_revision": current["mapping_revision"]}
        expect_rejection(call, "/commands", envelope(refreshed, keys=["W"], hold_ms=100), "INVALID_INPUT")
        before = call("/observations")
        assert run_command(call, envelope(refreshed), {"keys": ["I"], "hold_ms": 150}) == "completed"
        after = call("/observations")
        events = call(f"/events?after_seq={before['observation_seq']}")["events"]
        assert after["pawn_position"][0] > before["pawn_position"][0]
        assert after["held_keys"] == []
        assert any(event["kind"] == "Triggered" and event["value"] == "IA_Move" for event in events)
        assert [(event["kind"], event["value"]) for event in events if event["layer"] == "L1"] == [
            ("KeyDown", "I"), ("KeyUp", "I"),
        ]
        call("/session/stop", envelope(refreshed))
    print(f"PASS: mapping {initial['mapping_revision']} -> {current['mapping_revision']}, stale revision rejected, I moved the pawn")


if __name__ == "__main__":
    main()
