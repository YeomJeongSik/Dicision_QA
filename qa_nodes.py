"""Resolve a developer-defined input node into timed key transitions."""

MAX_NODE_MS = 3000
MAX_STEPS = 24


def number(value, default, lower, upper):
    if not isinstance(value, dict) or value.keys() - {"min", "default", "max", "value"}:
        raise ValueError("INVALID_NUMBER_RANGE")
    minimum = value.get("min", lower)
    maximum = value.get("max", upper)
    default = value.get("default", default)
    chosen = value.get("value", default)
    if (any(type(n) is not int for n in (minimum, maximum, default, chosen))
            or not lower <= minimum <= default <= maximum <= upper
            or not minimum <= chosen <= maximum):
        raise ValueError("INVALID_NUMBER_RANGE")
    return chosen


def compile_node(steps, mappings, catalog):
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError("INVALID_NODE_STEPS")
    at = 0
    transitions = []
    held = {}

    def edge(kind, keys):
        for key in keys:
            if (key in held) != (kind == "up"):
                raise ValueError("INVALID_KEY_TRANSITION")
            if kind == "down":
                held[key] = None
            else:
                del held[key]
            transitions.append((at, kind, key))

    for index, step in enumerate(steps, 1):
        try:
            if not isinstance(step, dict):
                raise ValueError("INVALID_NODE_STEP")
            kind = step.get("kind")
            if kind in ("key_tap", "key_hold", "key_down", "key_up"):
                kind = kind[4:]
            if kind == "parallel":
                kind = "hold"
            if kind not in ("tap", "hold", "down", "up", "wait"):
                raise ValueError("INVALID_STEP_KIND")
            default, lower = {"tap": (80, 20), "hold": (300, 20),
                              "down": (0, 0), "up": (0, 0), "wait": (100, 0)}[kind]
            timing = step.get("duration_ms", {})
            if type(timing) is int:
                timing = {"default": timing}
            duration = number(timing, default, lower, 2000)
            keys = step.get("keys")
            if "fkey" in step:
                if "keys" in step:
                    raise ValueError("INVALID_INPUT_REFERENCE")
                keys = [step["fkey"]]
            if "logical_action" in step:
                action = step["logical_action"]
                if keys is not None or not isinstance(action, str) or action not in catalog:
                    raise ValueError("INVALID_INPUT_REFERENCE")
                keys = [key for key, input_action in mappings.items() if input_action == catalog[action]]
                if len(keys) != 1:
                    raise ValueError("ACTION_MAPPING_MISSING_OR_AMBIGUOUS")
            if kind == "wait":
                if keys is not None:
                    raise ValueError("WAIT_HAS_KEYS")
                at += duration
            else:
                if (not isinstance(keys, list) or not keys
                        or any(not isinstance(key, str) or key not in mappings for key in keys)
                        or len(set(keys)) != len(keys)):
                    raise ValueError("UNMAPPED_OR_DUPLICATE_KEY")
                if kind in ("down", "up"):
                    edge(kind, keys)
                    at += duration
                else:
                    repeat = number(step.get("repeat", {}), 1, 1, 10) if kind == "tap" else 1
                    gap = number(step.get("gap_ms", {}), 50, 0, 2000) if kind == "tap" else 0
                    for attempt in range(repeat):
                        if attempt:
                            at += gap
                        edge("down", keys)
                        at += duration
                        edge("up", keys)
            if at > MAX_NODE_MS:
                raise ValueError("NODE_DURATION_LIMIT")
        except ValueError as error:
            raise ValueError(f"STEP_{index}: {error}") from None
    edge("up", list(held))
    return at, transitions
