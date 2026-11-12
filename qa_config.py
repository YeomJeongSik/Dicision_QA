"""Load bounded, data-only QA presets, catalogs and scenarios."""

import json
from pathlib import Path

from qa_mock import Rejected


def read_config(path):
    path = Path(path)
    with path.open("rb") as file:
        data = file.read(8193)
    if len(data) > 8192:
        raise Rejected("CONFIG_TOO_LARGE")
    if path.suffix.lower() not in (".yaml", ".yml"):
        return json.loads(data)
    try:
        import yaml
    except ImportError:
        raise Rejected("YAML_PARSER_REQUIRED") from None
    try:
        for event in yaml.parse(data, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.events.AliasEvent) or getattr(event, "anchor", None):
                raise Rejected("YAML_ALIASES_UNSUPPORTED")
        return yaml.safe_load(data)
    except (yaml.YAMLError, RecursionError):
        raise Rejected("INVALID_YAML_CONFIG") from None
