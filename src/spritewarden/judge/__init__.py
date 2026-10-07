"""Unified Quality Assurance Suite for Pixel Art and Animations."""

from pathlib import Path
from PIL import Image

from .models import GateResult, StillScorecard, AnimationScorecard
from .pixel import check_pixel_grid
from .palette import check_palette, remap_image, load_palette
from .bounds import check_clipping, check_symmetry
from .animation import check_animation_anomalies
from .walk import check_walk_cycle
from .loop import check_loop_smoothness, check_gif_timing, drop_loop_stalls
from .start_image import check_start_image, SIZE_TIERS
from .views import check_direction_set, load_direction_frames, GAME_ORDER
from .vision import VisionJudge, VisionObservation, OrchestratedVerdict, ReferenceComparison, reference_gate
from .walk import LOCOMOTION_MODES

__all__ = [
    "GateResult",
    "StillScorecard",
    "AnimationScorecard",
    "VisionObservation",
    "OrchestratedVerdict",
    "ReferenceComparison",
    "reference_gate",
    "LOCOMOTION_MODES",
    "VisionJudge",
    "check_pixel_grid",
    "check_palette",
    "remap_image",
    "load_palette",
    "check_clipping",
    "check_symmetry",
    "check_animation_anomalies",
    "check_walk_cycle",
    "check_loop_smoothness",
    "check_gif_timing",
    "check_start_image",
    "SIZE_TIERS",
    "check_direction_set",
    "load_direction_frames",
    "GAME_ORDER",
    "drop_loop_stalls",
    "evaluate_still",
    "evaluate_animation",
]


def evaluate_still(
    image_or_path: str | Path | Image.Image,
    palette_source: str | Path | list[str] | None = None,
    scale: int | None = None,
    verify_symmetry: bool = False,
    marker_colors: list[str] | None = None,
    save_masks_dir: str | Path | None = None,
    start_image: bool = False,
    target_height: int | None = None,
    size_tier: str | None = None,
    margin_px: int = 3,
    facing: str = "south",
    identity=None,
) -> StillScorecard:
    """Run all relevant checks on a still image or candidate frame.

    ``start_image=True`` adds the readiness gate for an anchor image (square canvas,
    transparent margin, symmetry for south/north, height vs tier/target, stray
    components, native pixels, palette); ``identity`` (a CharacterIdentity) is
    optional and supplies the palette, cell size and feature marker colours.
    """
    path_str = str(image_or_path) if isinstance(image_or_path, (str, Path)) else "<PIL.Image>"
    if isinstance(image_or_path, (str, Path)):
        img = Image.open(image_or_path).convert("RGBA")
    else:
        img = image_or_path.convert("RGBA")

    masks: dict[str, str] = {}
    m_dir = Path(save_masks_dir) if save_masks_dir else None
    if m_dir:
        m_dir.mkdir(parents=True, exist_ok=True)

    grid_mask_path = m_dir / "grid_mask.png" if m_dir else None
    palette_mask_path = m_dir / "palette_mask.png" if m_dir else None

    # Gate 1: Pixel Grid
    grid_gate, _ = check_pixel_grid(img, scale=scale, save_mask_path=grid_mask_path)
    if grid_mask_path:
        masks["grid"] = str(grid_mask_path)

    # Gate 2: Palette
    pal_gate, _ = check_palette(img, palette_source=palette_source, save_mask_path=palette_mask_path)
    if palette_mask_path:
        masks["palette"] = str(palette_mask_path)

    # Gate 3: Clipping / Slicing
    clip_gate = check_clipping(img)

    gates = [grid_gate, pal_gate, clip_gate]

    # Optional Gate 4: Symmetry
    if verify_symmetry:
        sym_gate = check_symmetry(img, marker_colors=marker_colors)
        gates.append(sym_gate)

    # Optional Gate 5: Start-image readiness (anchor for rotation / animation)
    if start_image:
        gates.append(
            check_start_image(
                img,
                target_height=target_height,
                size_tier=size_tier,
                margin_px=margin_px,
                facing=facing,
                palette=palette_source,
                identity=identity,
            )
        )

    overall_passed = all(g.passed for g in gates)
    avg_score = round(sum(g.score for g in gates) / len(gates), 3)

    failed_names = [g.name for g in gates if not g.passed]
    summary = (
        f"Passed all {len(gates)} still gates (score: {avg_score})"
        if overall_passed
        else f"Failed {len(failed_names)} gate(s): {', '.join(failed_names)}"
    )

    return StillScorecard(
        path=path_str,
        passed=overall_passed,
        score=avg_score,
        summary=summary,
        gates=gates,
        mask_paths=masks,
    )


def slice_sheet(sheet: Image.Image, cell_w: int, cell_h: int | None = None) -> list[Image.Image]:
    """Slice a spritesheet into a flat sequence of frame images."""
    cell_h = cell_h or cell_w
    frames = []
    rows = sheet.height // cell_h
    cols = sheet.width // cell_w
    for r in range(rows):
        for c in range(cols):
            f = sheet.crop((c * cell_w, r * cell_h, (c + 1) * cell_w, (r + 1) * cell_h))
            frames.append(f)
    return frames


def evaluate_animation(
    frames_or_sheet: list[Image.Image] | Image.Image | str | Path,
    cell: int | None = None,
    is_walk: bool = False,
    palette_source: str | Path | list[str] | None = None,
    ratio: float = 1.8,
    locomotion: str = "legs",
) -> AnimationScorecard:
    """Run all verification gates on an animation sequence.

    ``locomotion`` (legs | wheels | treads) selects the walk gate's mode when ``is_walk``:
    legs check alternating feet; wheels/treads check a steady ground row and a stable,
    turning base instead of cadence.
    """
    if isinstance(frames_or_sheet, (str, Path)):
        p = Path(frames_or_sheet)
        if p.is_dir():
            names = sorted([f for f in p.iterdir() if f.suffix.lower() == ".png"])
            frames = [Image.open(f).convert("RGBA") for f in names]
            label = str(p)
        else:
            sheet_img = Image.open(p).convert("RGBA")
            if not cell:
                raise ValueError("cell dimension required when evaluating a spritesheet file")
            frames = slice_sheet(sheet_img, cell, cell)
            label = str(p)
    elif isinstance(frames_or_sheet, Image.Image):
        if not cell:
            raise ValueError("cell dimension required when evaluating a spritesheet Image")
        frames = slice_sheet(frames_or_sheet, cell, cell)
        label = f"<SpriteSheet {frames_or_sheet.size}>"
    else:
        frames = [f.convert("RGBA") for f in frames_or_sheet]
        label = f"<{len(frames)} frames>"

    gates: list[GateResult] = []
    suggested_actions: list[str] = []

    # 1. Palette gate across all frames
    if palette_source:
        from collections import Counter
        pal_results = [check_palette(f, palette_source=palette_source)[0] for f in frames]
        bad_frames = [i for i, r in enumerate(pal_results) if not r.passed]
        pal_passed = len(bad_frames) == 0
        pal_score = round(sum(r.score for r in pal_results) / len(pal_results), 3)
        gates.append(
            GateResult(
                name="palette_consistency",
                passed=pal_passed,
                score=pal_score,
                summary="All frames strictly on-palette" if pal_passed else f"{len(bad_frames)} frame(s) off-palette",
                errors=[f"Off-palette colors in frame(s): {bad_frames[:5]}"] if not pal_passed else [],
                metrics={"off_palette_frames": bad_frames},
            )
        )

    # 2. Clipping gate across all frames
    clip_results = [check_clipping(f) for f in frames]
    sliced_frames = [i for i, r in enumerate(clip_results) if not r.passed]
    clip_passed = len(sliced_frames) == 0
    gates.append(
        GateResult(
            name="frame_clipping",
            passed=clip_passed,
            score=1.0 if clip_passed else 0.0,
            summary="No clipped or cut frame boundaries" if clip_passed else f"Clipped art in frame(s): {sliced_frames}",
            errors=[f"Canvas boundaries sliced in frame(s): {sliced_frames}"] if not clip_passed else [],
            metrics={"clipped_frames": sliced_frames},
        )
    )

    # 3. Temporal anomaly gate (specks, spikes, jitter)
    anom_gate = check_animation_anomalies(frames)
    gates.append(anom_gate)

    # 4. Loop smoothness gate
    loop_gate, retime_frames = check_loop_smoothness(frames, ratio=ratio)
    gates.append(loop_gate)
    if not loop_gate.passed and len(retime_frames) < len(frames):
        suggested_actions.append(f"Retime animation to frames {retime_frames} to eliminate hesitation stalls")

    # 5. Optional walk cycle gate
    if is_walk:
        walk_gate = check_walk_cycle(frames, locomotion=locomotion)
        gates.append(walk_gate)
        if not walk_gate.passed:
            if locomotion == "legs":
                suggested_actions.append("Adjust walk stride so left and right feet alternate lift height")
            else:
                suggested_actions.append(f"Pin the {locomotion} base: same ground row and hub box in every frame, only the tread/rim turns")

    overall_passed = all(g.passed for g in gates)
    avg_score = round(sum(g.score for g in gates) / len(gates), 3)
    failed_names = [g.name for g in gates if not g.passed]
    summary = (
        f"Animation passed all {len(gates)} QA gates ({len(frames)} frames, score {avg_score})"
        if overall_passed
        else f"Animation failed: {', '.join(failed_names)}"
    )

    return AnimationScorecard(
        path_or_count=label,
        frame_count=len(frames),
        passed=overall_passed,
        score=avg_score,
        summary=summary,
        gates=gates,
        suggested_actions=suggested_actions,
    )
