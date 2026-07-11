"""Resolve allowed root directories for file access."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def resolve_allowed_roots(file_access: str, workspace: Path) -> list[Path] | None:
    """Resolve allowed root directories based on ``file_access`` config value.

    Returns ``None`` when all paths are allowed (mode ``"all"``).
    Falls back to ``[workspace]`` (most restrictive) for unrecognized values.
    Supports a comma-separated list of paths or keywords (e.g. "workspace, ~/.gemini/antigravity-cli").
    """
    if not file_access:
        return [workspace]

    if file_access == "all":
        return None
    if file_access == "home":
        return [Path.home()]
    if file_access == "workspace":
        return [workspace]

    # Support custom comma-separated paths or keywords
    if "," in file_access or "/" in file_access or file_access.startswith("~"):
        roots = []
        for part in file_access.split(","):
            part = part.strip()
            if not part:
                continue
            if part == "workspace":
                roots.append(workspace)
            elif part == "home":
                roots.append(Path.home())
            else:
                try:
                    path = Path(part).expanduser().resolve()
                    roots.append(path)
                except Exception as e:
                    logger.warning("Failed to resolve custom allowed root %r: %s", part, e)
        if roots:
            return roots

    logger.warning(
        "Unknown file_access value %r, falling back to workspace-only",
        file_access,
    )
    return [workspace]
