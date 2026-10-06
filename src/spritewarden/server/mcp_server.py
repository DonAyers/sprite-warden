"""MCP server exposing slaygent art repository, QA judges, and smart routing to agents."""

import json
from pathlib import Path
from typing import Annotated
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..judge import evaluate_still, evaluate_animation, VisionJudge
from ..judge.vision import DEFAULT_VISION_MODEL
from ..tools.aseprite import open_file, find_aseprite
from ..repo.catalog import AssetCatalog
from ..repo.identity import CharacterIdentity
from ..repo.palette import registry as palettes
from ..reducer import ReduceParams, pixelize, refine
from ..router import PipelineRouter
from ..sheet import build_sheets, engine_report, pad_files
from ..learning.db import LearningDB
from ..learning.doctor import report as learn_report
from ..learning.episodes import record_episode as _record_episode
from ..learning.models import Episode
from ..learning.proposals import review as _review_proposal
from ..learning.taxonomy import normalize_perspective

catalog = AssetCatalog()
router = PipelineRouter(catalog=catalog)

INSTRUCTIONS = """\
spritewarden is the intelligent pixel art pipeline and quality assurance tool.
Use it to:
1. Search existing art before generating (`search_art`).
2. Retrieve or register consistent Character Identities (`get_character_identity`, `register_character_identity`).
3. Formulate optimal generation recipes with Jev and OpenRouter Fusion (`route_generation`).
4. Validate pixel art stills against mixels, palette, and clipping (`judge_still`).
5. Validate animations for temporal specks, spikes, and loop stutter (`judge_animation`).
6. Reduce any artwork to a pixel palette render, AI-refined on request (`pixelize_image`).
7. Open sprites and sheets in Aseprite for visual inspection (`open_in_aseprite`).
8. Consult the self-learning knowledge base: live resources `spritewarden://playbook`,
   `spritewarden://proposals`, `spritewarden://providers`, `spritewarden://learn-status`;
   tools `learn_status`, `list_learned_proposals`, `approve_learned_proposal`, `record_learning_episode`.
"""

mcp = MCPServer("spritewarden", instructions=INSTRUCTIONS)


@mcp.tool()
def search_art(
    query: Annotated[str, Field(description="Search terms (e.g. 'robot walk', 'laser turret', 'dungeon crate')")] = "",
    game: Annotated[str | None, Field(description="Game project slug (e.g. 'i-require-maintenance', 'smol-experiment')")] = None,
    character_id: Annotated[str | None, Field(description="Filter by CharacterIdentity slug")] = None,
    perspective: Annotated[str | None, Field(description="side, top-down, low top-down, three-quarter, isometric")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict:
    """Search the central repository for existing pixel art assets to prevent duplicate generation."""
    results = catalog.search(query=query, game=game, character_id=character_id, perspective=perspective, limit=limit)
    return {"count": len(results), "results": results}


@mcp.tool()
def get_character_identity(character_id: Annotated[str, Field(description="Character slug, e.g. 'smolbot', 'tredbot'")]) -> dict:
    """Retrieve the canonical CharacterIdentity (palette, proportions, visual prompt, and constraints)."""
    ident = catalog.get_identity(character_id)
    if not ident:
        return {"error": f"Character '{character_id}' not found"}
    return ident.model_dump()


@mcp.tool()
def register_character_identity(
    id: Annotated[str, Field(description="Unique slug, e.g. 'smolbot'")],
    name: Annotated[str, Field(description="Display name")],
    prompt_description: Annotated[str, Field(description="Core visual description")],
    game: Annotated[str, Field(description="Game project")] = "global",
    palette_id: Annotated[str, Field(description="Assigned palette")] = "luap-40",
    perspective: Annotated[str, Field(description="Camera perspective")] = "low top-down",
    width: Annotated[int, Field(description="Standard cell width")] = 64,
    height: Annotated[int, Field(description="Standard cell height")] = 64,
    prohibitions: Annotated[list[str], Field(description="Explicitly forbidden traits")] = [],
) -> dict:
    """Create or update a canonical CharacterIdentity to ensure cross-view visual consistency."""
    ident = CharacterIdentity(
        id=id,
        name=name,
        game=game,
        palette_id=palette_id,
        perspective=perspective,
        dimensions=[width, height],
        prompt_description=prompt_description,
        prohibitions=prohibitions,
    )
    catalog.save_identity(ident)
    return {"status": "ok", "identity": ident.model_dump()}


@mcp.tool()
def route_generation(
    prompt: Annotated[str, Field(description="What needs to be generated")],
    asset_type: Annotated[str, Field(description="character, vehicle, tileset, props, icon, vfx")] = "character",
    character_id: Annotated[str | None, Field(description="CharacterIdentity slug if applicable")] = None,
    game: Annotated[str, Field(description="Target game")] = "global",
    perspective: Annotated[str, Field(description="Perspective view")] = "low top-down",
    size: Annotated[int, Field(description="Pixel resolution size (16, 32, 64, 96)")] = 64,
    is_animation: Annotated[bool, Field(description="True if generating an animation loop")] = False,
    is_vehicle_or_rigid: Annotated[bool, Field(description="True for vehicles or rigid objects")] = False,
    force_fusion: Annotated[bool, Field(description="Force multi-model OpenRouter Fusion deliberation")] = False,
) -> dict:
    """Determine the optimal generator provider and prompt recipe using Jev or OpenRouter Fusion."""
    char_ident = catalog.get_identity(character_id) if character_id else None
    result = router.route_request(
        prompt=prompt,
        asset_type=asset_type,
        character_identity=char_ident,
        game=game,
        perspective=perspective,
        size=size,
        is_animation=is_animation,
        is_vehicle_or_rigid=is_vehicle_or_rigid,
        force_fusion=force_fusion,
    )
    return result.model_dump()


@mcp.tool()
def build_sprite_sheet(
    paths: Annotated[list[str], Field(description="Image files, folders, or glob patterns")],
    output: Annotated[str, Field(description="Output PNG path, or a directory when group_by='prefix'")] = "spritesheet.png",
    palette: Annotated[str | None, Field(description="Palette name / lospec slug / file, or null to skip palette snapping")] = "luap-40",
    cell: Annotated[int | None, Field(description="Fixed cell size in px (default: auto = max content)")] = None,
    columns: Annotated[int | None, Field(description="Grid columns (default: near-square)")] = None,
    snap: Annotated[bool, Field(description="Run grid snapping (Rust snapper if available, else Python fallback)")] = True,
    engine: Annotated[str, Field(description="auto, rust, python, or none")] = "auto",
    align: Annotated[str, Field(description="top/center/bottom + left/center/right")] = "bottom-center",
    margin: Annotated[int, Field(description="Padding inside each cell (px)")] = 0,
    power_of_two: Annotated[bool, Field(description="Round the cell size up to a power of two")] = False,
    crop: Annotated[bool, Field(description="Alpha-crop each frame to its content bounds")] = True,
    group_by: Annotated[str, Field(description="'none' or 'prefix' (one sheet per filename prefix)")] = "none",
    recursive: Annotated[bool, Field(description="Recurse into folders")] = False,
) -> dict:
    """Combine loose frames or a folder into a cohesive, palette-snapped, evenly-framed spritesheet."""
    reports = build_sheets(
        paths,
        output,
        group_by=group_by,
        palette=palette,
        cell=cell,
        columns=columns,
        snap=snap,
        engine=engine,
        align=align,
        margin=margin,
        power_of_two=power_of_two,
        crop=crop,
        recursive=recursive,
    )
    return {"sheets": [r.model_dump() for r in reports], "engine": engine_report()}


@mcp.tool()
def pad_sprite(
    paths: Annotated[list[str], Field(description="Image files, folders, or glob patterns")],
    size: Annotated[int | None, Field(description="Square canvas size in px (e.g. 64)")] = None,
    canvas: Annotated[str | None, Field(description="WxH canvas (e.g. '96x64'); overrides size")] = None,
    grow: Annotated[int | None, Field(description="Add N px of padding on every side of each frame's content")] = None,
    align: Annotated[str, Field(description="top/center/bottom + left/center/right (default bottom-center)")] = "bottom-center",
    margin: Annotated[int, Field(description="Padding between content and canvas edge (px)")] = 0,
    power_of_two: Annotated[bool, Field(description="Round the canvas up to a power of two")] = False,
    fit: Annotated[str, Field(description="Oversized content: 'clip' at canvas edge or 'shrink' (integer NEAREST downscale)")] = "clip",
    crop_content: Annotated[bool, Field(description="Alpha-crop to content bounds before placing")] = True,
    palette: Annotated[str | None, Field(description="Optional palette to snap to in the same pass")] = None,
    output: Annotated[str | None, Field(description="Output PNG (single input) or directory (batch)")] = None,
    in_place: Annotated[bool, Field(description="Overwrite the input files")] = False,
    recursive: Annotated[bool, Field(description="Recurse into folders")] = False,
) -> dict:
    """Normalize provider output onto a uniform, aligned canvas (providers return varying sizes hugging the edges)."""
    report = pad_files(
        paths,
        out=output,
        size=size,
        canvas=canvas,
        grow=grow,
        align=align,
        margin=margin,
        power_of_two=power_of_two,
        fit=fit,
        crop_content=crop_content,
        palette=palette,
        in_place=in_place,
        recursive=recursive,
    )
    return report.model_dump()


@mcp.tool()
def judge_still(
    file_path: Annotated[str, Field(description="Path to PNG image to evaluate")],
    palette: Annotated[str, Field(description="Target palette name (default: luap-40)")] = "luap-40",
    scale: Annotated[int | None, Field(description="Expected pixel scale factor, or null to infer")] = None,
    check_symmetry_axis: Annotated[bool, Field(description="Check silhouette symmetry for front/back poses")] = False,
    vision: Annotated[bool, Field(description="Enable multimodal vision smell-test and Jev decision orchestration")] = False,
    vision_model: Annotated[str, Field(description="Vision model slug (default: google/gemini-2.5-flash)")] = DEFAULT_VISION_MODEL,
    description: Annotated[str, Field(description="Expected visual description of the subject")] = "",
    perspective: Annotated[str, Field(description="Expected perspective view")] = "low top-down",
) -> dict:
    """Run pixel grid, palette conformity, edge clipping, and symmetry gates on a still image."""
    card = evaluate_still(
        image_or_path=file_path,
        palette_source=palette,
        scale=scale,
        verify_symmetry=check_symmetry_axis,
    )
    out = card.model_dump()
    if vision:
        from PIL import Image
        img = Image.open(file_path).convert("RGBA")
        judge = VisionJudge(vision_model=vision_model)
        verdict = judge.orchestrate_judgment(
            still_scorecard=card,
            image=img,
            prompt_description=description,
            expected_perspective=perspective,
        )
        out["orchestrated_verdict"] = verdict.model_dump()
    return out


@mcp.tool()
def judge_animation(
    sheet_or_dir_path: Annotated[str, Field(description="Path to spritesheet PNG or directory of frame PNGs")],
    cell_size: Annotated[int | None, Field(description="Square cell size for spritesheet, e.g. 64 or 96")] = None,
    is_walk: Annotated[bool, Field(description="Run walk cadence alternating-foot analysis")] = False,
    palette: Annotated[str | None, Field(description="Target palette to check")] = "luap-40",
) -> dict:
    """Run temporal anomaly, loop smoothness, and walk cadence checks on an animation sequence."""
    card = evaluate_animation(
        frames_or_sheet=sheet_or_dir_path,
        cell=cell_size,
        is_walk=is_walk,
        palette_source=palette,
    )
    return card.model_dump()


@mcp.tool()
def open_in_aseprite(
    file_path: Annotated[str, Field(description="Path to PNG or .aseprite file to open for inspection")],
) -> dict:
    """Launch Aseprite on the host system to view and inspect an asset."""
    try:
        proc = open_file(file_path)
        return {"status": "ok", "message": f"Opened {file_path} in Aseprite (PID {proc.pid})"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@mcp.tool()
def pixelize_image(
    file_path: Annotated[str, Field(description="Path to the source image (any resolution)")],
    palette: Annotated[str, Field(description="Palette: builtin name, lospec slug (e.g. 'na16'), 'lospec:<slug>', or a file path")] = "luap-40",
    width: Annotated[int | None, Field(description="Target pixel width (aspect-preserving); omit to auto-search the width ladder")] = None,
    dither: Annotated[str, Field(description="none, bayer2, bayer4, bayer8, floyd, or sierra")] = "none",
    dither_strength: Annotated[float, Field(ge=0.0, le=1.5)] = 1.0,
    downscale: Annotated[str, Field(description="lanczos, box, nearest, or dominant")] = "lanczos",
    saturation: Annotated[float, Field(ge=0.5, le=1.6)] = 1.0,
    brightness: Annotated[float, Field(ge=0.5, le=1.6)] = 1.0,
    contrast: Annotated[float, Field(ge=0.5, le=1.6)] = 1.0,
    refine_params: Annotated[bool, Field(description="Run the iterative refinement loop (vision-guided when use_vision)")] = False,
    max_rounds: Annotated[int, Field(ge=1, le=8)] = 4,
    use_vision: Annotated[bool, Field(description="Judge candidates with the OpenRouter vision model")] = False,
    vision_model: Annotated[str, Field(description="Vision model slug")] = DEFAULT_VISION_MODEL,
) -> dict:
    """Reduce any image to palette-snapped pixel art; optionally AI-refine the mapping."""
    params = ReduceParams(
        width=width,
        dither=dither,  # type: ignore[arg-type]
        dither_strength=dither_strength,
        downscale=downscale,  # type: ignore[arg-type]
        saturation=saturation,
        brightness=brightness,
        contrast=contrast,
    )
    if not refine_params:
        result = pixelize(file_path, palette, params)
        return {
            "params": result.params.model_dump(),
            "output_size": [result.image.width, result.image.height],
            "source_size": list(result.source_size),
            "usage": [{"hex": h, "count": c} for h, c in result.usage],
            "palette": result.palette_hex,
            "note": "Image was rendered in-process; use the CLI (warden pixelize) for saved files.",
        }
    report = refine(
        image_or_path=file_path,
        palette_source=palette,
        params=params,
        max_rounds=max_rounds,
        use_vision=use_vision,
        vision_model=vision_model,
    )
    return report.model_dump()


# ---------------------------------------------------------------------------
# Learning subsystem: live resources (always-current strategy state)
# ---------------------------------------------------------------------------


@mcp.resource("spritewarden://playbook", name="playbook", description="Active learned strategies", mime_type="application/json")
def resource_playbook() -> str:
    """Approved (active) strategies the router can apply, grouped by scope."""
    db = LearningDB()
    return json.dumps({"strategies": [s.model_dump() for s in db.list_strategies(status="active")]}, indent=2)


@mcp.resource("spritewarden://proposals", name="proposals", description="Open learned-change proposals awaiting review", mime_type="application/json")
def resource_proposals() -> str:
    db = LearningDB()
    return json.dumps({"proposals": [p.model_dump() for p in db.list_proposals(status="open")]}, indent=2)


@mcp.resource("spritewarden://providers", name="providers", description="Observed provider tools, limits, and watched sources", mime_type="application/json")
def resource_providers() -> str:
    db = LearningDB()
    return json.dumps(
        {
            "sources": [s.model_dump() for s in db.list_sources()],
            "tools": [t.model_dump() for t in db.list_tools()],
        },
        indent=2,
    )


@mcp.resource("spritewarden://learn-status", name="learn-status", description="Learning knowledge-base health and staleness", mime_type="application/json")
def resource_learn_status() -> str:
    return json.dumps(learn_report(LearningDB()), indent=2)


@mcp.tool()
def learn_status() -> dict:
    """Report learning knowledge-base health: sources, events, episodes, strategies, proposals, budget."""
    return learn_report(LearningDB())


@mcp.tool()
def list_learned_proposals(
    status: Annotated[str, Field(description="open / approved / rejected (default: open)")] = "open",
) -> dict:
    """List self-improvement proposals (never auto-applied; review before acting)."""
    db = LearningDB()
    return {"proposals": [p.model_dump() for p in db.list_proposals(status=status)]}


@mcp.tool()
def approve_learned_proposal(
    proposal_id: Annotated[int, Field(description="Proposal id to approve or reject")],
    approve: Annotated[bool, Field(description="True to approve (applies change), False to reject")] = True,
) -> dict:
    """Approve/reject a learned proposal; approval activates a strategy or acknowledges a doc change."""
    proposal = _review_proposal(LearningDB(), proposal_id, approve)
    if proposal is None:
        return {"status": "error", "error": f"proposal {proposal_id} not found"}
    return {"status": "ok", "proposal": proposal.model_dump()}


@mcp.tool()
def record_learning_episode(
    provider: Annotated[str, Field(description="Generation provider, e.g. pixellab, sprite_fusion, pixelize")],
    operation: Annotated[str, Field(description="Operation, e.g. create_character_v3, direction-set, refine")],
    outcome: Annotated[str, Field(description="accepted / rejected / rerolled / pending")],
    asset_type: Annotated[str, Field(description="character, vehicle, tileset, props, icon")] = "sprite",
    perspective: Annotated[str, Field(description="top-down / angled-top-down / side-on / isometric")] = "unknown",
    game: Annotated[str, Field(description="Project slug")] = "global",
    reason: Annotated[str, Field(description="Why it succeeded/failed (evidence)")] = "",
) -> dict:
    """Record a generation attempt's outcome as a learning episode (the reward signal)."""
    perspective = normalize_perspective(perspective).canonical or perspective
    eid = _record_episode(
        LearningDB(),
        Episode(provider=provider, operation=operation, outcome=outcome, asset_type=asset_type,
                perspective=perspective, game=game, reason=reason),
    )
    return {"status": "ok", "episode_id": eid}


def run_mcp_server() -> None:
    try:
        from ..learning.scheduler import LearningScheduler

        LearningScheduler().start(daemon=True)
    except Exception:
        pass
    mcp.run()
