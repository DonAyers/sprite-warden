"""Base generator driver interface for slaygent asset generation."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field

from ..router.playbook import GenerationRecipe


class GenerationOutput(BaseModel):
    """The result of an asset generation attempt."""
    success: bool
    provider: str
    output_files: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float | None = None
    error: str | None = None


class BaseGeneratorDriver(ABC):
    """Abstract driver for invoking a pixel art generation provider."""

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if the necessary API keys or credentials are set."""
        pass

    @abstractmethod
    def generate(self, recipe: GenerationRecipe, output_dir: str | Path) -> GenerationOutput:
        """Execute generation according to the supplied recipe."""
        pass

    def fetch_asset(self, asset_id: str, output_dir: str | Path, **params: Any) -> GenerationOutput:
        """Download an already-existing provider asset by its id, when the provider supports it.

        The default is unsupported; drivers override it for the providers that expose a
        fetch-by-id/download surface (PixelLab bundles, SpriteCook assets). This is what
        ``warden fetch`` calls to pull provider work down locally.
        """
        return GenerationOutput(
            success=False,
            provider=getattr(self, "provider_name", type(self).__name__),
            error=f"provider does not support fetch-by-id (asset_id={asset_id!r})",
        )

    def fetch_all(self, output_dir: str | Path, **params: Any) -> GenerationOutput:
        """Download the provider's whole account asset library, when it exposes a listing.

        The default is unsupported; Sprite Fusion overrides it (``GET /assets`` is
        cursor-paginated). This is what ``warden fetch <provider> --all`` calls.
        """
        return GenerationOutput(
            success=False,
            provider=getattr(self, "provider_name", type(self).__name__),
            error="provider does not expose an account asset listing (fetch by id instead)",
        )
