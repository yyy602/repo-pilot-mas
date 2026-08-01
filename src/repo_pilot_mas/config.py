"""YAML configuration loading for the Phase 2 local baseline."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


def load_yaml(path: str | Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load RepoPilot-MAS configuration") from exc

    config_path = Path(path).expanduser().resolve(strict=True)
    value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise TypeError("configuration file must contain one mapping")
    return dict(value)
