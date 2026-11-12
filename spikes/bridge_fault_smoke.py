"""Check game-defect and crash verdicts using the isolated fault fixture."""

import argparse
import json
import time
from pathlib import Path
from unittest.mock import patch

from qa import bridge_test, wait_for_ue_world


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--defect-report", type=Path)
    args = parser.parse_args()
    def settled_world(call):
        wait_for_ue_world(call)
        time.sleep(2)

    # Let the fixture's initial graphics pipeline compilation finish before sending input.
    with patch("qa.wait_for_ue_world", settled_world):
        check_faults(args.executable, args.defect_report)


def check_faults(executable, saved):
    defect = (json.loads(saved.read_text(encoding="utf-8")) if saved
              else bridge_test(executable, ["SpaceBar"], 120, ["IA_Jump"]))
    assert defect["verdict"] == "FAIL", defect["run_id"]
    assert any(event["kind"] == "KeyDown" and event["value"] == "SpaceBar" for event in defect["events"])
    assert not any(event["kind"] == "JumpRise" for event in defect["events"])
    print(f"PASS: disabled jump with dispatched SpaceBar classified FAIL ({defect['run_id']})", flush=True)

    crash = bridge_test(executable, None, None, ["IA_Jump"], steps=[
        {"kind": "down", "keys": ["W"], "duration_ms": 80},
        {"kind": "tap", "keys": ["SpaceBar"], "duration_ms": 100},
        {"kind": "up", "keys": ["W"]},
    ])
    assert crash["verdict"] == "ERROR" and crash.get("game_exit_code") not in (None, 0), crash["run_id"]
    assert "QA_FIXTURE_INTENTIONAL_CRASH" in Path(crash["engine_log"]).read_text(encoding="utf-8", errors="replace")
    assert crash.get("crash_dumps") and all(Path(name).stat().st_size > 0 for name in crash["crash_dumps"]), crash["run_id"]
    print(f"PASS: game crash reported exit {crash['game_exit_code']}, engine log and nonempty dump ({crash['run_id']})")


if __name__ == "__main__":
    main()
