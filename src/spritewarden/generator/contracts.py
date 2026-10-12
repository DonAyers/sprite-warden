"""Local execution contracts, distinct from advisory recipes/import capabilities."""
from pathlib import Path

SOURCE_KEYS = ("inputs", "input_image", "input", "reference_image", "start_image", "source_image", "image")

# Existence of a driver or recipe is NOT evidence that a native workflow executes.
EXECUTABLE = {
    "spritefusion": {"generate", "edit", "animate", "direction-set", "style-reference"},
    "spritecook": {"generate_game_art", "edit", "animate_game_art"},
    "openrouter_image": {"generate"},
}


def capability_status(provider: str, operation: str) -> dict:
    from ..router.recipes import registry
    reg = registry()
    canonical = reg.canonical_provider(provider)
    entry = reg.get(provider, operation)
    status = "unsupported"
    reason = "Unknown provider/operation contract"
    if entry:
        if entry.operation in EXECUTABLE.get(canonical, set()):
            status, reason = "executable", "Implemented REST operation with required output validation"
        elif canonical == "pixellab":
            status, reason = "import_only", "PixelLab driver downloads existing bundles; native generation is not implemented"
        else:
            status, reason = "advisory", "Recipe guidance exists, but this native workflow has no executable driver"
    return {"provider": canonical or provider, "operation": entry.operation if entry else operation,
            "status": status, "executable": status == "executable", "reason": reason,
            "transport": "rest" if status == "executable" else None}


def source_paths(params: dict, root: str | Path | None = None) -> list[Path]:
    """One source representation for validation and serialization; conflicting aliases refuse."""
    candidates = []
    for key in SOURCE_KEYS:
        value = params.get(key)
        if value is None or value == "" or value == []:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        paths = []
        for item in values:
            if not isinstance(item, (str, Path)) or str(item).startswith(("http://", "https://", "data:")):
                raise ValueError("source must be a local image path; import remote images first")
            path = Path(item)
            if root is not None and not path.is_absolute():
                path = Path(root) / path
            paths.append(path.resolve())
        candidates.append(paths)
    if not candidates:
        return []
    if any(paths != candidates[0] for paths in candidates[1:]):
        raise ValueError("conflicting source aliases: provide one canonical input")
    for path in candidates[0]:
        if not path.is_file():
            raise ValueError(f"source image not found locally: {path}")
    return candidates[0]
