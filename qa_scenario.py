"""Validate and evaluate the generic scripted scenario subset."""

import math

from qa_mock import Rejected


def scenario_nodes(document, mappings):
    if (not isinstance(document, dict) or document.get("schema_version") not in ("1.0", 1)
            or document.get("mode", "scripted") != "scripted"
            or not isinstance(document.get("id"), str) or not 1 <= len(document["id"]) <= 64):
        raise Rejected("INVALID_SCENARIO")
    actions = document.get("actions")
    if not isinstance(actions, list) or not 1 <= len(actions) <= 24:
        raise Rejected("INVALID_SCENARIO_ACTIONS")
    nodes = []
    seen = set()
    catalog = mappings["catalog"]
    for action in actions:
        if not isinstance(action, dict):
            raise Rejected("INVALID_SCENARIO_ACTION")
        name, logical = action.get("id"), action.get("do")
        if (not isinstance(name, str) or not 1 <= len(name) <= 64 or name in seen
                or not isinstance(logical, str) or logical not in catalog
                or ("hold_ms" in action and "press_ms" in action)):
            raise Rejected("INVALID_SCENARIO_ACTION")
        seen.add(name)
        assertions = action.get("assertions", [])
        if not isinstance(assertions, list) or len(assertions) > 16:
            raise Rejected("INVALID_SCENARIO_ASSERTIONS")
        for assertion in assertions:
            if not isinstance(assertion, dict) or not isinstance(assertion.get("type"), str):
                raise Rejected("INVALID_SCENARIO_ASSERTION")
            if assertion["type"] == "position_delta_min":
                threshold = assertion.get("value")
                if type(threshold) not in (int, float) or not 0 <= threshold <= 1e12:
                    raise Rejected("INVALID_SCENARIO_ASSERTION")
        kind = "hold" if "hold_ms" in action else "tap"
        duration = action.get("hold_ms", action.get("press_ms", 100))
        nodes.append((name, [{"kind": kind, "logical_action": logical, "duration_ms": duration}], assertions))
    return nodes


def evaluate_assertions(assertions, before, after, events, catalog, status):
    results = []
    for assertion in assertions:
        kind = assertion["type"]
        verdict = "INCONCLUSIVE"
        if kind == "input_action_event":
            logical = assertion.get("action")
            target = catalog.get(logical) if isinstance(logical, str) else None
            phase = assertion.get("phase", "Triggered")
            if target and phase in ("Triggered", "Completed") and events is not None:
                verdict = "PASS" if any(event["kind"] == phase and event["value"] == target for event in events) else "FAIL"
        elif kind == "position_delta_min" and assertion.get("axis", "horizontal") == "horizontal":
            start, end = before.get("pawn_position"), after.get("pawn_position")
            if all(isinstance(value, list) and len(value) == 3
                   and all(type(item) in (int, float) and math.isfinite(item) for item in value)
                   for value in (start, end)):
                verdict = "PASS" if math.dist(start[:2], end[:2]) >= assertion["value"] else "FAIL"
            else:
                start, end = before.get("position"), after.get("position")
                if type(start) in (int, float) and type(end) in (int, float) and math.isfinite(end - start):
                    verdict = "PASS" if end - start >= assertion["value"] else "FAIL"
        elif kind == "no_crash" and status == "completed":
            verdict = "PASS"
        results.append({"type": kind, "verdict": verdict})
    return results
