"""Configuration and local secret-environment loading."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


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


def load_env_file(
    path: str | Path,
    *,
    required_names: tuple[str, ...],
) -> dict[str, str]:
    """Load declared secrets without overriding an explicit process environment."""

    for name in required_names:
        if not _ENV_NAME.fullmatch(name):
            raise ValueError(f"invalid environment variable name: {name!r}")
    env_candidate = Path(path).expanduser()
    if env_candidate.is_symlink():
        raise ValueError("credential file must not be a symbolic link")
    env_path = env_candidate.resolve(strict=False)
    file_values: dict[str, str] = {}
    if env_path.exists():
        if env_path.is_symlink() or not env_path.is_file():
            raise ValueError("credential file must be one regular file")
        if env_path.stat().st_mode & 0o077:
            raise PermissionError("credential file permissions must not allow group or other access")
        for line_number, raw_line in enumerate(
            env_path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                raise ValueError(f"invalid credential file line: {line_number}")
            name, raw_value = line.split("=", 1)
            name = name.strip()
            if name not in required_names:
                raise ValueError(f"credential file contains undeclared variable: {name}")
            if name in file_values:
                raise ValueError(f"credential file contains duplicate variable: {name}")
            value = _unquote(raw_value.strip())
            if not value:
                raise ValueError(f"credential value must not be empty: {name}")
            file_values[name] = value

    loaded: dict[str, str] = {}
    for name in required_names:
        value = os.environ.get(name) or file_values.get(name)
        if not value:
            raise RuntimeError(f"required credential environment variable is missing: {name}")
        os.environ.setdefault(name, value)
        loaded[name] = value
    return loaded


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
