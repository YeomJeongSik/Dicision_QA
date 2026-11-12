"""Developer approvals and runtime mapping filters for Decisions choices."""

import re

from qa_config import read_config
from qa_mock import Rejected

BUILTIN_ACTIONS = {"MOVE": ("IA_Move",), "JUMP": ("IA_Jump",), "SPRINT": ("IA_Move", "IA_Sprint")}
STATES = ("DISCOVERED", "NEEDS_CONFIRMATION", "USER_CONFIRMED", "ADAPTER_VERIFIED", "UNSUPPORTED")
SCOPES = ("selected_actions", "selected_keys", "all_mapped")


def discovered_actions(mappings, observation):
    """Expose only unambiguous actions the UE bridge currently allows executing."""
    if observation.get("player_exists") is not True:
        return {}
    bindings = {}
    for key, action in mappings["keys"].items():
        if isinstance(action, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", action) and action != "WAIT":
            bindings.setdefault(action, []).append(key)
    return {
        action: (keys, action, f"Exercise Unreal Input Action {action}, currently mapped to {keys[0]}.")
        for action, keys in bindings.items()
        if len(keys) == 1 and (action != "IA_Jump" or observation.get("on_ground") is True)
    }


def read_catalog(path):
    if path is None:
        return {name: {"input_actions": actions, "semantic_status": "ADAPTER_VERIFIED",
                       "description": actions[-1], "ai_allowed": True, "prerequisites": []}
                for name, actions in BUILTIN_ACTIONS.items()}
    catalog = read_config(path)
    if not isinstance(catalog, dict) or catalog.get("catalog_version") != "1.0":
        raise Rejected("INVALID_ACTION_CATALOG")
    rows = catalog.get("actions")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 24:
        raise Rejected("INVALID_ACTION_CATALOG")
    records = {}
    for row in rows:
        if not isinstance(row, dict):
            raise Rejected("INVALID_ACTION_CATALOG")
        name = row.get("id")
        actions = row.get("input_actions")
        description = row.get("description")
        conditions = row.get("prerequisites", [])
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,47}", name)
                or name in records or name == "WAIT"
                or not isinstance(actions, list) or not 1 <= len(actions) <= 3
                or any(not isinstance(action, str) or not 1 <= len(action) <= 200 for action in actions)
                or not isinstance(description, str) or not 1 <= len(description.strip()) <= 140
                or row.get("semantic_status") not in STATES or type(row.get("ai_allowed")) is not bool
                or not isinstance(conditions, list)
                or any(condition not in ("player_exists", "on_ground") for condition in conditions)):
            raise Rejected("INVALID_ACTION_CATALOG")
        records[name] = {**row, "prerequisites": conditions}
    return records


def action_candidates(allowed, mappings, catalog, scope, selected_keys, observation):
    if scope not in SCOPES:
        raise Rejected("INVALID_TEST_SCOPE")
    names = allowed if allowed is not None or scope == "selected_actions" else list(catalog)
    if not isinstance(names, list) or not names or any(
            not isinstance(name, str) or name not in catalog for name in names):
        raise Rejected("NO_VALID_ACTION")
    mapping = mappings["keys"]
    if scope == "selected_keys":
        if (not isinstance(selected_keys, list) or not selected_keys
                or any(not isinstance(key, str) or key not in mapping for key in selected_keys)):
            raise Rejected("INVALID_SELECTED_KEYS")
        mapping = {key: mapping[key] for key in selected_keys}
    conditions = {"player_exists": observation.get("player_exists") is True,
                  "on_ground": observation.get("on_ground") is True}
    available = {}
    for name in names:
        action = catalog[name]
        if (not action["ai_allowed"] or action["semantic_status"] not in ("USER_CONFIRMED", "ADAPTER_VERIFIED")
                or not all(conditions[c] for c in action["prerequisites"])):
            continue
        bindings = [[key for key, target in mapping.items() if target == input_action]
                    for input_action in action["input_actions"]]
        if not all(len(keys) == 1 for keys in bindings):
            continue
        keys = [matches[0] for matches in bindings]
        if len(set(keys)) == len(keys):
            available[name] = (keys, action["input_actions"][-1], action["description"])
    if not available:
        raise Rejected("NO_VALID_ACTION")
    return available
