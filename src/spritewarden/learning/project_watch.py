"""Project-state watcher: register a game project's own files as watched sources.

This addresses skill/tool drift against the *project* (palettes, theme .tres,
asset conventions) by reusing the acquisition/diff pipeline via `kind="file"`.
"""

from __future__ import annotations

from pathlib import Path

from .db import LearningDB
from .models import Source

DEFAULT_PATTERNS = ["**/*.tres", "**/*.hex", "**/*.pal", "**/palette*.json", "**/.spritewarden.json"]


def register_project(
    db: LearningDB,
    provider: str,
    root: str | Path,
    patterns: list[str] | None = None,
    cadence_s: int = 86400,
) -> int:
    """Register every file matching `patterns` under `root` as a watched project source."""
    root_path = Path(root)
    if not root_path.is_dir():
        raise FileNotFoundError(f"Project root not found: {root}")
    globs = patterns or DEFAULT_PATTERNS
    count = 0
    for pattern in globs:
        for path in sorted(root_path.glob(pattern)):
            if not path.is_file():
                continue
            rel = path.relative_to(root_path).as_posix()
            source = Source(
                id=f"{provider}:{rel}",
                provider=provider,
                kind="file",
                url=str(path.resolve()),
                cadence_s=cadence_s,
                notes=f"Project state watcher ({rel})",
            )
            db.upsert_source(source)
            count += 1
    return count
