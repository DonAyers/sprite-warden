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
