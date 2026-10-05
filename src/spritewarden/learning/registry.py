"""Seeded registry of provider documentation, API, and tool surfaces to watch."""

from __future__ import annotations

from .db import LearningDB
from .models import Source

KNOWN_PROVIDERS = [
    "pixellab",
    "sprite_fusion",
    "retro_diffusion",
    "openrouter_image",
    "meshy",
    "aseprite",
    "lospec",
]

SEED_SOURCES: list[Source] = [
    Source(
        id="openrouter_models",
        provider="openrouter_image",
        kind="json_api",
        url="https://openrouter.ai/api/v1/models",
        cadence_s=43200,
        notes="Model availability + pricing surface; pure JSON structural diff.",
    ),
    Source(
        id="pixellab_docs",
        provider="pixellab",
        kind="llms_txt",
        url="https://www.pixellab.ai/llms.txt",
        cadence_s=86400,
        notes="PixelLab LLM-friendly docs index (falls back to logging a fetch failure event).",
    ),
    Source(
        id="spritefusion_docs",
        provider="sprite_fusion",
        kind="llms_txt",
        url="https://www.spritefusion.com/llms.txt",
        cadence_s=86400,
        notes="Sprite Fusion docs index / API guidance.",
    ),
    Source(
        id="retrodiffusion_docs",
        provider="retro_diffusion",
        kind="llms_txt",
        url="https://www.retrodiffusion.ai/llms.txt",
        cadence_s=86400,
        notes="Retro Diffusion docs index (rd_* endpoint guidance).",
    ),
    Source(
        id="aseprite_releases",
        provider="aseprite",
        kind="rss",
        url="https://github.com/aseprite/aseprite/releases.atom",
        cadence_s=86400,
        notes="Aseprite release notes feed (sheet/palette Lua API changes).",
    ),
    Source(
        id="pixellab_mcp_tools",
        provider="pixellab",
        kind="mcp_tools",
        url="mcp://pixellab",
        cadence_s=86400,
        notes="PixelLab MCP tool surface (tools/list drift); executed by the L3 scheduler.",
    ),
]


def seed_sources(db: LearningDB) -> int:
    """Idempotently register the built-in source set. Returns the number seeded."""
    for source in SEED_SOURCES:
        db.upsert_source(source)
    return len(SEED_SOURCES)
