"""Project configuration for the enforcement core: ``spritewarden.json`` at the project root.

::

    {
      "manifest": "art_source/spritecook-assets.json",
      "override": "tmp/generator_hook_override.json",
      "cli_tools": {"tools/sprite_fusion.py": "spritefusion", "tools/retro_diffusion.py": "retrodiffusion"},
      "review_specs": true
    }

Every key is optional; the defaults above apply when the file or a key is absent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_NAME = "spritewarden.json"
DEFAULT_MANIFEST = "art_source/spritecook-assets.json"
DEFAULT_OVERRIDE = "tmp/generator_hook_override.json"
DEFAULT_CLI_TOOLS = {
    "tools/sprite_fusion.py": "spritefusion",
    "tools/retro_diffusion.py": "retrodiffusion",
}


@dataclass
class ProjectConfig:
    root: Path
    manifest: str = DEFAULT_MANIFEST
    override: str = DEFAULT_OVERRIDE
    cli_tools: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_CLI_TOOLS))
    review_specs: bool = True
    error: str | None = None  # a malformed spritewarden.json: defaults are used and the error is reported

    @property
    def manifest_path(self) -> Path:
        return self.resolve(self.manifest)

    @property
    def override_path(self) -> Path:
        return self.resolve(self.override)

    def resolve(self, path: str | Path) -> Path:
        p = Path(path)
        return p if p.is_absolute() else self.root / p

    def relative(self, path: str | Path) -> str:
        """A path for messages: relative to the project root when inside it, forward slashes."""
        p = Path(path)
        try:
            return p.resolve().relative_to(self.root.resolve()).as_posix()
        except (ValueError, OSError):
            return p.as_posix()


def load_config(project_root: str | Path) -> ProjectConfig:
    root = Path(project_root)
    cfg = ProjectConfig(root=root)
    path = root / CONFIG_NAME
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return cfg
    try:
        raw: Any = json.loads(raw_text)
        if not isinstance(raw, dict):
            raise ValueError("top level must be an object")
        if isinstance(raw.get("manifest"), str):
            cfg.manifest = raw["manifest"]
        if isinstance(raw.get("override"), str):
            cfg.override = raw["override"]
        if "cli_tools" in raw:
            tools = raw["cli_tools"]
            if not isinstance(tools, dict):
                raise ValueError("cli_tools must be an object of {script path: provider}")
            cfg.cli_tools = {str(k).replace("\\", "/"): str(v) for k, v in tools.items()}
        if "review_specs" in raw:
            cfg.review_specs = bool(raw["review_specs"])
    except ValueError as exc:
        cfg.error = f"{CONFIG_NAME}: {exc}"
    return cfg


def find_project_root(start: str | Path) -> Path:
    """Nearest ancestor of ``start`` holding ``spritewarden.json``, else the nearest with ``.git``, else ``start``."""
    start = Path(start)
    chain = [start, *start.parents]
    for marker in (CONFIG_NAME, ".git"):
        for d in chain:
            try:
                if (d / marker).exists():
                    return d
            except OSError:
                continue
    return start
