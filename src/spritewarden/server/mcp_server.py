"""MCP server exposing the spritewarden art repository, QA judges, and smart routing to agents.

Startup is hardened so the server always handshakes: the catalog and router are
created lazily on first use, heavy optional imports are guarded, and the
learning scheduler is skipped when ``SPRITEWARDEN_NO_LEARN=1``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..judge import evaluate_still, evaluate_animation, VisionJudge
from ..judge.vision import DEFAULT_VISION_MODEL
from ..gates.judging import judgment_operation
from ..repo.identity import CharacterIdentity

_catalog: Any = None
_router: Any = None


def get_catalog():
    """Create the AssetCatalog on first use (SQLite file creation is deferred past startup)."""
    global _catalog
    if _catalog is None:
        from ..repo.catalog import AssetCatalog

        _catalog = AssetCatalog()
    return _catalog


def get_router():
    """Create the PipelineRouter on first use."""
    global _router
    if _router is None:
        from ..router import PipelineRouter

        _router = PipelineRouter(catalog=get_catalog())
    return _router


def learning_disabled() -> bool:
    return os.environ.get("SPRITEWARDEN_NO_LEARN", "").strip().lower() in ("1", "true", "yes", "on")


INSTRUCTIONS = """\
spritewarden is the intelligent pixel art pipeline and quality assurance tool.
Use it to:
1. Search existing art before generating (`search_art`).
2. Retrieve or register consistent Character Identities (`get_character_identity`, `register_character_identity`).
3. Formulate optimal generation recipes with Jev and OpenRouter Fusion (`route_generation`).
4. Validate pixel art stills against mixels, palette, and clipping (`judge_still`).
5. Validate animations for temporal specks, spikes, and loop stutter (`judge_animation`).
5b. Gate an anchor before deriving anything from it (`judge_still` with start_image=true) and a
   direction set before animating it (`judge_direction_set`).
6. Reduce any artwork to a pixel palette render, AI-refined on request (`pixelize_image`).
7. Open sprites and sheets in Aseprite for visual inspection (`open_in_aseprite`).
8. Read each provider's documented recipe before generating (`get_recipe`, resource `spritewarden://recipes`):
   start-image rules, forbidden words, argument limits, and the gates required before/after.
9. Consult the self-learning knowledge base: live resources `spritewarden://playbook`,
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
    results = get_catalog().search(query=query, game=game, character_id=character_id, perspective=perspective, limit=limit)
    return {"count": len(results), "results": results}


@mcp.tool()
def get_character_identity(character_id: Annotated[str, Field(description="Character slug, e.g. 'smolbot', 'tredbot'")]) -> dict:
    """Retrieve the canonical CharacterIdentity (palette, proportions, visual prompt, and constraints)."""
    ident = get_catalog().get_identity(character_id)
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
    features: Annotated[list[dict] | None, Field(description="Landmark features: [{name, side: 'left'|'right'|null (the character's own side, for a held tool), marker_hex: '#ff00ff', relative_box: [x, y, w, h] on the front cell, description, rigid}]. Sided features with a marker_hex enable judge_direction_set's tool-side check. Merged by name into an existing identity.")] = None,
    turnaround_stills: Annotated[dict[str, str] | None, Field(description="Approved view per facing, e.g. {'south': 'art/bot/south.png'}; the south still is the default reference for judge_still")] = None,
    ground_row_offset: Annotated[int | None, Field(description="Pixels up from the bottom cell edge where the feet / wheel rest")] = None,
    style_anchors: Annotated[dict | None, Field(description="Provider ids that pin the look, e.g. {'spritecook': '<asset_id>', 'pixellab_character_id': '...'}")] = None,
    locomotion: Annotated[str | None, Field(description="legs | wheels | treads: the walk gate's mode when judge_animation gets this character_id")] = None,
    body_class: Annotated[str | None, Field(description="figure (head/torso/limbs) | rigid (vehicle, prop)")] = None,
    side_front_area_ratio: Annotated[list[float] | None, Field(description="Opt-in inclusive east/west opaque area / south area [MIN, MAX]; finite 0 < MIN <= MAX <= 1")] = None,
    size_tier: Annotated[str | None, Field(description="Resolution tier of the set: 16-32 | 48-64 | 96 | 128 (the start-image height check when judge_still gets this character_id)")] = None,
) -> dict:
    """Create or update a canonical CharacterIdentity to ensure cross-view visual consistency. On an existing id the optional fields left null are kept; features, stills and style anchors merge by key."""
    from ..repo.identity import build_identity

    catalog = get_catalog()
    try:
        ident = build_identity(
            catalog.get_identity(id),
            id,
            name=name,
            game=game,
            palette_id=palette_id,
            perspective=perspective,
            dimensions=[width, height],
            prompt_description=prompt_description,
            prohibitions=prohibitions or None,
            features=features,
            turnaround_stills=turnaround_stills,
            ground_row_offset=ground_row_offset,
            style_anchors=style_anchors,
            locomotion=locomotion,
            body_class=body_class,
            size_tier=size_tier,
            side_front_area_ratio=side_front_area_ratio,
        )
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
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
    char_ident = get_catalog().get_identity(character_id) if character_id else None
    result = get_router().route_request(
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
    from ..sheet import build_sheets, engine_report

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
    from ..sheet import pad_files

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
@judgment_operation
def judge_still(
    file_path: Annotated[str, Field(description="Path to PNG image to evaluate")],
    palette: Annotated[str, Field(description="Target palette name (default: luap-40)")] = "luap-40",
    scale: Annotated[int | None, Field(description="Expected pixel scale factor, or null to infer")] = None,
    check_symmetry_axis: Annotated[bool, Field(description="Check silhouette symmetry for front/back poses")] = False,
    vision: Annotated[bool, Field(description="Enable multimodal vision smell-test and Jev decision orchestration")] = False,
    vision_model: Annotated[str, Field(description="Vision model slug (default: google/gemini-2.5-flash)")] = DEFAULT_VISION_MODEL,
    description: Annotated[str, Field(description="Expected visual description of the subject")] = "",
    perspective: Annotated[str, Field(description="Expected perspective view")] = "low top-down",
    start_image: Annotated[bool, Field(description="Readiness gate for an anchor/start image: square canvas, transparent margin, symmetry (south/north), drawn height vs tier/target, stray components, native 1:1 pixels, palette")] = False,
    target_height: Annotated[int | None, Field(description="Expected drawn height in px (+-10%) for start_image")] = None,
    size_tier: Annotated[str | None, Field(description="Resolution tier for start_image: 16-32, 48-64, 96, 128")] = None,
    margin_px: Annotated[int, Field(description="Required transparent margin for start_image (default 3)")] = 3,
    facing: Annotated[str, Field(description="Facing of the start image (south/north enable symmetry)")] = "south",
    character_id: Annotated[str | None, Field(description="CharacterIdentity slug supplying palette, cell size and feature markers")] = None,
    write_gate_record: Annotated[bool, Field(description="Write <image>.gate.json beside the image (sha256 + verdicts) so later stages can prove it was gated")] = True,
    reference_path: Annotated[str | None, Field(description="Approved anchor image: the vision model compares candidate vs anchor side by side (parts added/removed/moved, which hand holds which tool, camera elevation). Anomalies or a perspective mismatch FAIL; offline FAILS (never a silent accept). Defaults to the identity's south turnaround still when character_id is given.")] = None,
) -> dict:
    """Run pixel grid, palette conformity, edge clipping, symmetry, (opt-in) start-image readiness and (opt-in) reference-comparison gates on a still image."""
    from ..gates import gates_from_scorecard, write_record
    from ..judge import reference_gate

    identity = get_catalog().get_identity(character_id) if character_id else None
    if not reference_path and identity is not None and identity.turnaround_stills.get("south"):
        candidate_ref = identity.turnaround_stills["south"]
        if Path(candidate_ref).is_file():
            reference_path = candidate_ref
    card = evaluate_still(
        image_or_path=file_path,
        palette_source=palette,
        scale=scale,
        verify_symmetry=check_symmetry_axis,
        start_image=start_image,
        target_height=target_height,
        size_tier=size_tier,
        margin_px=margin_px,
        facing=facing,
        identity=identity,
    )
    verdict = None
    if vision or reference_path:
        from PIL import Image
        img = Image.open(file_path).convert("RGBA")
        judge = VisionJudge(vision_model=vision_model)
    if reference_path:
        ref_img = Image.open(reference_path).convert("RGBA")
        from ..judge.views import canonical_facing

        comparison = judge.compare_to_reference(
            img, ref_img,
            facing=canonical_facing(facing) or facing,
            anchor_facing="south",
            description=description,
            expected_perspective=perspective,
        )
        ref_gate = reference_gate(comparison, facing=facing)
        card.gates.append(ref_gate)
        card.passed = card.passed and ref_gate.passed
    out = card.model_dump()
    if reference_path:
        out["reference"] = reference_path
    if vision:
        verdict = judge.orchestrate_judgment(
            still_scorecard=card,
            image=img,
            prompt_description=description,
            expected_perspective=perspective,
        )
        out["orchestrated_verdict"] = verdict.model_dump()
    overall = bool(card.passed) and (verdict.passed if verdict else True)
    out["passed"] = bool(overall)
    gates = gates_from_scorecard(card)
    if verdict:
        gates["vision"] = {"passed": bool(verdict.passed), "score": round(verdict.production_ready_prob, 3),
                           "summary": verdict.reasoning_summary}
    gates["still"] = {"passed": bool(overall)}
    try:
        out["gate_record"] = str(write_record(file_path, gates, params={
            "command": "judge_still", "palette": palette, "start_image": start_image, "target_height": target_height,
            "size_tier": size_tier, "margin_px": margin_px, "facing": facing, "character_id": character_id, "vision": vision,
            "reference": reference_path, "identity": identity.model_dump() if identity else None,
        }, persist=write_gate_record))
        if not write_gate_record:
            out.pop("gate_record", None)
    except Exception as exc:  # noqa: BLE001
        out["gate_record_error"] = str(exc)
    return out


@mcp.tool()
@judgment_operation
def judge_direction_set(
    source_path: Annotated[str, Field(description="Folder of <facing>.png files (south.png, down_right.png, ...) or a strip PNG in game order (down, down_right, right, up_right, up, up_left, left, down_left)")],
    cell_size: Annotated[int | None, Field(description="Cell size when the source is a strip (default: its height when it is N square cells)")] = None,
    anchor_path: Annotated[str | None, Field(description="Anchor image to compare against (default: the south view)")] = None,
    palette: Annotated[str | None, Field(description="Target palette to check, or null to skip")] = "luap-40",
    character_id: Annotated[str | None, Field(description="CharacterIdentity slug; sided marker features (e.g. 'left hand wrench' with marker_hex) enable the tool-side check")] = None,
    write_gate_record: Annotated[bool, Field(description="Write <image>.gate.json beside each view (or the strip)")] = True,
    vision: Annotated[bool, Field(description="Compare views against anchor; exact completed evidence is reused, cache misses are paid. Invented/missing-visible parts fail; expected occlusion does not. Fresh unavailable evidence fails.")] = False,
    vision_model: Annotated[str, Field(description="Vision model slug")] = DEFAULT_VISION_MODEL,
    area_tolerance: Annotated[float, Field(description="Opaque-area tolerance, finite in [0, 1); also symmetric max/min east/west difference with an identity profile policy")] = 0.20,
    description: Annotated[str, Field(description="Subject description for the vision comparison")] = "",
    refresh_vision: Annotated[bool, Field(description="Explicit paid recheck; report-only, requires write_gate_record=false. Prior default evidence is preserved.")] = False,
    anchor_facing: Annotated[str, Field(description="Facing of the external anchor (default south); profile anchors must be explicitly east or west")] = "south",
) -> dict:
    """Cross-view gate for a direction set: same ground row (+-1), drawn height (+-2px), opaque area (+-20%), equal component count (extra parts are named), tool side per facing, palette."""
    from PIL import Image

    from ..gates import gates_from_scorecard, write_record
    from ..judge import check_direction_set, load_direction_frames
    from ..judge.views import direction_frame_files

    if refresh_vision and (write_gate_record or not vision):
        raise ValueError('Vision refresh is report-only: requires vision=true and write_gate_record=false')
    identity = get_catalog().get_identity(character_id) if character_id else None
    frames = load_direction_frames(source_path, cell=cell_size)
    anchor = Image.open(anchor_path).convert("RGBA") if anchor_path else None
    gate = check_direction_set(frames, anchor=anchor, anchor_facing=anchor_facing, identity=identity, palette=palette, area_tolerance=area_tolerance)
    out = {"source": source_path, "views": list(frames), **gate.model_dump()}
    vision_gates: dict = {}
    vision_gate = None
    if vision:
        from ..judge.views import canonical_facing, compare_views_to_anchor, vision_summary_gate

        anchor_facing = canonical_facing(anchor_facing) or anchor_facing
        vision_anchor = anchor
        skip = False
        if vision_anchor is None and identity is not None:
            identity_anchor = identity.turnaround_stills.get(anchor_facing)
            if identity_anchor and Path(identity_anchor).is_file():
                anchor_path = str(identity_anchor)
                vision_anchor = Image.open(anchor_path).convert("RGBA")
        if vision_anchor is None:
            vision_anchor = next((img for name, img in frames.items() if (canonical_facing(name) or name) == anchor_facing), None)
            skip = True
        if vision_anchor is None:
            return {**out, "passed": False, "error": f"vision needs an anchor: pass anchor_path or include a {anchor_facing} view"}
        from ..enforce.core import find_pipeline
        pipeline, _ = find_pipeline(Path(source_path))
        pipeline = pipeline or {}
        faces = pipeline.get("faces") if isinstance(pipeline.get("faces"), dict) else None
        vision_gates = compare_views_to_anchor(
            frames, vision_anchor, VisionJudge(vision_model=vision_model), anchor_facing=anchor_facing, skip_anchor_view=skip,
            description=description or str(pipeline.get("description") or "") or (identity.prompt_description if identity is not None else ""),
            faces=faces, refresh=refresh_vision,
        )
        vision_gate = vision_summary_gate(vision_gates)
        out["vision_reference"] = {"summary": vision_gate.model_dump(),
                                   "views": {f: g.model_dump() for f, g in vision_gates.items()}}
        out["passed"] = bool(gate.passed) and vision_gate.passed
    src = Path(source_path)
    files = direction_frame_files(src) if src.is_dir() else {}
    targets = list(files.values()) if src.is_dir() else [src]
    by_path = {str(p): f for f, p in files.items()}
    gates = gates_from_scorecard(gate)
    gates["views"] = {"passed": bool(out["passed"]), "facings": list(frames)}
    if vision_gate is not None and not src.is_dir():
        gates.update(gates_from_scorecard(vision_gate))
    params = {"command": "judge_direction_set", "cell": cell_size, "anchor": anchor_path, "anchor_facing": anchor_facing, "palette": palette,
              "character_id": character_id, "vision": vision, "direction_set": source_path,
              "area_tolerance": area_tolerance, "side_front_area_ratio": identity.side_front_area_ratio if identity else None,
              "identity": identity.model_dump() if identity else None}
    written = []
    for t in targets:
        try:
            own = gates
            facing_gate = vision_gates.get(by_path.get(str(t), ""))
            if facing_gate is not None:
                own = {**gates, **gates_from_scorecard(facing_gate)}
            target = write_record(t, own, params={**params, "facing": by_path.get(str(t))}, persist=write_gate_record)
            if write_gate_record:
                written.append(str(target))
        except Exception as exc:  # noqa: BLE001
            out.setdefault("gate_record_errors", []).append(f"{t}: {exc}")
    out["gate_records"] = written
    return out


@mcp.tool()
def review_check(
    paths: Annotated[list[str], Field(description="Image files, folders, or glob patterns")],
    require: Annotated[list[str], Field(description="Gate names that must have passed; any one satisfies (start_image, views, anim, still, direction_set, ...)")] = ["still"],
    read_only: Annotated[bool, Field(description="Inspect evidence including failed/rejected candidates; never grants promotion readiness")] = False,
) -> dict:
    """Inspect candidates freely, or check strict transition readiness (the default)."""
    from ..gates import require_passed
    if read_only:
        from ..gates.records import _expand, load_record
        files = _expand(paths)
        return {"ok": bool(files) and all(p.is_file() for p in files), "read_only": True,
                "evidence": [{"path": str(p), "record": load_record(p)} for p in files]}
    return require_passed(paths, require)


@mcp.tool()
@judgment_operation
def judge_animation(
    sheet_or_dir_path: Annotated[str, Field(description="Path to spritesheet PNG or directory of frame PNGs")],
    cell_size: Annotated[int | None, Field(description="Square cell size for spritesheet, e.g. 64 or 96")] = None,
    is_walk: Annotated[bool, Field(description="Run the walk-mechanics gate (cadence for legs; steady turning base for wheels/treads)")] = False,
    palette: Annotated[str | None, Field(description="Target palette to check")] = "luap-40",
    locomotion: Annotated[str | None, Field(description="legs | wheels | treads: wheels/treads skip cadence and check ground-row steadiness and the ground-contact mass's box. Null = the character identity's locomotion, else legs")] = None,
    character_id: Annotated[str | None, Field(description="CharacterIdentity slug: supplies locomotion and the palette when they are not given")] = None,
    source_path: Annotated[str | None, Field(description="The still the animation was made from: the frame_integrity gate fails frames with enclosed transparent holes the still lacks (a keyed-out fill) or >15% less opaque area. Without it frames are only compared with each other, so a hole present in EVERY frame goes unseen.")] = None,
) -> dict:
    """Run frame integrity (holes / lost area vs the source still), temporal anomaly, loop smoothness, and walk mechanics checks on an animation sequence."""
    from ..gates import gates_from_scorecard, write_record

    identity = get_catalog().get_identity(character_id) if character_id else None
    locomotion = locomotion or (identity.locomotion if identity is not None else None) or "legs"
    card = evaluate_animation(
        frames_or_sheet=sheet_or_dir_path,
        cell=cell_size,
        is_walk=is_walk,
        palette_source=palette,
        locomotion=locomotion,
        source=source_path,
    )
    out = card.model_dump()
    src = Path(sheet_or_dir_path)
    targets = sorted(f for f in src.iterdir() if f.suffix.lower() == ".png") if src.is_dir() else [src]
    written = []
    for t in targets:
        try:
            written.append(str(write_record(t, gates_from_scorecard(card, "anim"), params={
                "command": "judge_animation", "cell": cell_size, "is_walk": is_walk, "locomotion": locomotion, "palette": palette,
                "source": source_path, "character_id": character_id,
            })))
        except Exception as exc:  # noqa: BLE001
            out.setdefault("gate_record_errors", []).append(f"{t}: {exc}")
    out["gate_records"] = written
    return out


@mcp.tool()
def preflight_repair(project_root: str, plan: dict, ask_jev: bool = False,
                     authorize_decision_spend: bool = False) -> dict:
    """Inspect repair intent, scope, real pixel differences and failed attempts before any edit.

    Default is read-only and free. Optional Jev advice requires explicit authorization
    for ONE decision query only; it never authorizes generation, acceptance or publication.
    Plan schema: source, goal, defect_kind, proposed_action; optional candidate, mask,
    required_regions {name:[x,y,width,height]}, preserved_regions, available_actions,
    spend_cap_usd, quoted_costs, request_fingerprint, attempts and max_paid_attempts.
    Do not treat a missing model decision or an assessment dispute as approval.
    """
    from ..repair_preflight import inspect_plan
    return inspect_plan(project_root,plan,ask_jev=ask_jev,authorize_decision_spend=authorize_decision_spend)


@mcp.tool()
def inspect_workflow(project_root: str, character_dir: str) -> dict:
    """Inspect current revisions and readiness without spending or promoting."""
    from ..workflow import inspect
    return inspect(project_root, character_dir)


@mcp.tool()
def plan_workflow(project_root: str, provider: str, operation: str, prompt: str,
                  params: dict, character_dir: str | None = None) -> dict:
    """Compile an exact provider contract and its dependency fingerprints; no paid calls."""
    from ..workflow import plan
    return plan(project_root, provider, operation, prompt, params, character_dir=character_dir)


@mcp.tool()
def execute_workflow(project_root: str, plan: dict, output_dir: str, authorize_spend: bool = False) -> dict:
    """Execute a current plan once. Explicit authorize_spend is required; never retry an unknown completion."""
    from ..workflow import execute
    return execute(project_root, plan, output_dir, authorize_spend=authorize_spend)


@mcp.tool()
def workflow_history(project_root: str, run_id: str | None = None, limit: int = 100) -> list:
    """Read immutable workflow events, including failed attempts and human decisions."""
    from ..workflow import history
    return history(project_root, run_id=run_id, limit=limit)


@mcp.tool()
def open_in_aseprite(
    file_path: Annotated[str, Field(description="Path to PNG or .aseprite file to open for inspection")],
) -> dict:
    """Launch Aseprite on the host system to view and inspect an asset."""
    try:
        from ..tools.aseprite import open_file

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
    from ..reducer import ReduceParams, pixelize, refine

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


@mcp.resource("spritewarden://recipes", name="recipes", description="Per-provider recipe registry: start-image rules, prompt rules, forbidden words, arg limits, required gates", mime_type="application/json")
def resource_recipes() -> str:
    from ..router.recipes import registry_json

    return registry_json()


@mcp.tool()
def get_recipe(
    provider: Annotated[str, Field(description="pixellab, spritefusion, retrodiffusion, spritecook, openrouter_image (aliases accepted)")],
    operation: Annotated[str | None, Field(description="Operation or alias (e.g. direction-set, rotate, create_character_v3); omit to list the provider's operations")] = None,
) -> dict:
    """Read the documented recipe for a provider operation: what its start image must look like, prompt rules, limits, and the gates required before and after."""
    from ..router.recipes import registry

    reg = registry()
    if reg.canonical_provider(provider) is None:
        return {"error": f"unknown provider '{provider}'", "known": list(reg.providers)}
    if operation:
        rec = reg.get(provider, operation)
        if rec is None:
            return {"error": f"unknown operation '{operation}'", "known": [r.operation for r in reg.select(provider)]}
        out = rec.model_dump()
        out["forbidden_words_effective"] = [p.pattern for p in reg.forbidden_patterns(provider, operation)]
        return out
    return {"provider": reg.canonical_provider(provider), "recipes": [r.model_dump() for r in reg.select(provider)]}


@mcp.resource("spritewarden://playbook", name="playbook", description="Active learned strategies", mime_type="application/json")
def resource_playbook() -> str:
    """Approved (active) strategies the router can apply, grouped by scope."""
    from ..learning.db import LearningDB

    db = LearningDB()
    return json.dumps({"strategies": [s.model_dump() for s in db.list_strategies(status="active")]}, indent=2)


@mcp.resource("spritewarden://proposals", name="proposals", description="Open learned-change proposals awaiting review", mime_type="application/json")
def resource_proposals() -> str:
    from ..learning.db import LearningDB

    db = LearningDB()
    return json.dumps({"proposals": [p.model_dump() for p in db.list_proposals(status="open")]}, indent=2)


@mcp.resource("spritewarden://providers", name="providers", description="Observed provider tools, limits, and watched sources", mime_type="application/json")
def resource_providers() -> str:
    from ..learning.db import LearningDB

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
    from ..learning.db import LearningDB
    from ..learning.doctor import report as learn_report

    return json.dumps(learn_report(LearningDB()), indent=2)


@mcp.tool()
def learn_status() -> dict:
    """Report learning knowledge-base health: sources, events, episodes, strategies, proposals, budget."""
    from ..learning.db import LearningDB
    from ..learning.doctor import report as learn_report

    return learn_report(LearningDB())


@mcp.tool()
def list_learned_proposals(
    status: Annotated[str, Field(description="open / approved / rejected (default: open)")] = "open",
) -> dict:
    """List self-improvement proposals (never auto-applied; review before acting)."""
    from ..learning.db import LearningDB

    db = LearningDB()
    return {"proposals": [p.model_dump() for p in db.list_proposals(status=status)]}


@mcp.tool()
def approve_learned_proposal(
    proposal_id: Annotated[int, Field(description="Proposal id to approve or reject")],
    approve: Annotated[bool, Field(description="True to approve (applies change), False to reject")] = True,
) -> dict:
    """Approve/reject a learned proposal; approval activates a strategy or acknowledges a doc change."""
    from ..learning.db import LearningDB
    from ..learning.proposals import review as _review_proposal

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
    from ..learning.db import LearningDB
    from ..learning.episodes import record_episode as _record_episode
    from ..learning.models import Episode
    from ..learning.taxonomy import normalize_perspective

    perspective = normalize_perspective(perspective).canonical or perspective
    eid = _record_episode(
        LearningDB(),
        Episode(provider=provider, operation=operation, outcome=outcome, asset_type=asset_type,
                perspective=perspective, game=game, reason=reason),
    )
    return {"status": "ok", "episode_id": eid}


def start_learning_scheduler() -> bool:
    """Start the background learning watcher unless disabled; never raises."""
    if learning_disabled():
        return False
    try:
        from ..learning.scheduler import LearningScheduler

        LearningScheduler().start(daemon=True)
        return True
    except Exception:
        return False


def run_mcp_server() -> None:
    start_learning_scheduler()
    mcp.run()
