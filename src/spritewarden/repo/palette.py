"""Palette Registry: centralized color palettes and cross-game color management."""

from pathlib import Path
from ..judge.palette import BUILTIN_PALETTES, load_palette, hex_to_rgb, rgb_to_hex


class PaletteRegistry:
    """Registry maintaining color palettes across games and characters."""

    def __init__(self):
        self._palettes: dict[str, list[tuple[int, int, int]]] = {}
        for name in BUILTIN_PALETTES:
            self._palettes[name] = load_palette(name)

    def register(self, name: str, source: str | Path | list[str] | list[tuple[int, int, int]]) -> None:
        """Register a new palette by name."""
        colors = load_palette(source)
        self._palettes[name.lower()] = colors

    def get(self, name: str) -> list[tuple[int, int, int]]:
        """Retrieve RGB tuple list for a registered palette."""
        key = name.lower()
        if key not in self._palettes:
            # Try loading directly if it is a path or hex list
            try:
                colors = load_palette(name)
                self._palettes[key] = colors
                return colors
            except Exception:
                raise KeyError(f"Palette '{name}' is not registered. Available: {list(self._palettes.keys())}")
        return self._palettes[key]

    def get_hex(self, name: str) -> list[str]:
        return [rgb_to_hex(c) for c in self.get(name)]

    def list_palettes(self) -> list[str]:
        return sorted(self._palettes.keys())


# Singleton instance
registry = PaletteRegistry()
