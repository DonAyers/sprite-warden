"""Central art repository, character identities, and palette management."""

from .identity import CharacterIdentity, SceneIdentity, FeatureLandmark
from .palette import PaletteRegistry, registry as palettes
from .catalog import AssetCatalog

__all__ = [
    "CharacterIdentity",
    "SceneIdentity",
    "FeatureLandmark",
    "PaletteRegistry",
    "palettes",
    "AssetCatalog",
]
