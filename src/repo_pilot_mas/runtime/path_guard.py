"""Path validation helpers that keep tools inside one repository root."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


class PathSecurityError(ValueError):
    """Raised when a requested path escapes or targets protected content."""


class PathGuard:
    """Resolve user-controlled relative paths against a trusted root."""

    def __init__(
        self,
        root: str | Path,
        *,
        protected_parts: Iterable[str] = (".git",),
    ) -> None:
        resolved = Path(root).expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError(f"root is not a directory: {resolved}")
        self.root = resolved
        self.protected_parts = frozenset(protected_parts)

    def resolve(
        self,
        value: str | Path = ".",
        *,
        must_exist: bool = True,
        allow_root: bool = True,
        allow_protected: bool = False,
    ) -> Path:
        raw = Path(value)
        if raw.is_absolute():
            raise PathSecurityError("absolute paths are not allowed")
        if any(part == ".." for part in raw.parts):
            raise PathSecurityError("parent-directory traversal is not allowed")

        candidate = (self.root / raw).resolve(strict=must_exist)
        try:
            common = Path(os.path.commonpath((str(self.root), str(candidate))))
        except ValueError as exc:
            raise PathSecurityError("path is outside the workspace") from exc
        if common != self.root:
            raise PathSecurityError("path is outside the workspace")

        relative = candidate.relative_to(self.root)
        if not allow_root and relative == Path("."):
            raise PathSecurityError("the workspace root is not allowed here")
        if not allow_protected and any(part in self.protected_parts for part in relative.parts):
            raise PathSecurityError("protected repository metadata cannot be accessed")
        return candidate

    def relative(self, value: str | Path) -> str:
        candidate = Path(value).resolve(strict=False)
        try:
            return candidate.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise PathSecurityError("path is outside the workspace") from exc
