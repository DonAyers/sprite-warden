"""The full command-line interface: pixel art QA judges, repository, routing, generation.

Imported by ``spritewarden.cli`` only when the subcommand is not one of the fast
enforcement commands (``hook``, ``check-call``), because the imports below are heavy.
"""

import argparse
import json
import sys
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.markup import escape

from .judge import evaluate_still, evaluate_animation, VisionJudge, SIZE_TIERS, LOCOMOTION_MODES
from .judge.vision import DEFAULT_VISION_MODEL
from .tools.aseprite import open_file, edit_sheet_interactive, find_aseprite
from .repo.catalog import AssetCatalog
from .repo.identity import CharacterIdentity
from .repo.palette import registry as palettes
from .reducer import ReduceParams, pixelize, refine
from .router import PipelineRouter
from .generator import available_providers, dispatch
from .learning import cli as learning_cli

console = Console()


def _auto_preview_scale(size: tuple[int, int]) -> int:
    return max(2, min(16, 512 // max(1, max(size))))


def cmd_pixelize(args: argparse.Namespace) -> int:
    from PIL import Image

    params = ReduceParams(
        width=args.width,
        height=args.height,
        downscale=args.downscale,
        pre_blur=args.pre_blur,
        saturation=args.saturation,
        brightness=args.brightness,
        contrast=args.contrast,
        dither=args.dither,
        dither_strength=args.strength,
        max_colors=args.max_colors,
        snap_guards=not args.no_guards,
        alpha=args.alpha,
    )
    if args.scale:
        with Image.open(args.image) as im:
            params.width = max(1, round(im.width / args.scale))
            params.height = max(1, round(im.height / args.scale))
        if args.preview_scale is None:
            args.preview_scale = args.scale

    if args.refine:
        report = refine(
            image_or_path=args.image,
            palette_source=args.palette,
            params=params,
            out_dir=args.session_dir,
            max_rounds=args.rounds,
            use_vision=args.vision,
            vision_model=args.vision_model,
            description=args.desc or "",
        )
        if args.learn:
            from .learning.db import LearningDB
            from .learning.episodes import record_from_refine

            try:
                record_from_refine(LearningDB(), report)
            except Exception as exc:
                console.print(f"[dim]Learning record skipped: {exc}[/]")
        out_path = Path(args.out) if args.out else Path(report.best_image)
        if Path(report.best_image) != out_path:
            out_path.write_bytes(Path(report.best_image).read_bytes())
            preview_src = Path(report.preview_image)
            preview_path = out_path.with_name(out_path.stem + ".preview.png")
            preview_path.write_bytes(preview_src.read_bytes())
        else:
            preview_path = Path(report.preview_image)
        preview_scale_used = args.preview_scale or _auto_preview_scale(Image.open(out_path).size)
        used_params = report.best.params
        det_metrics = report.best.det_metrics
        vision_notes = report.best.vision.notes if report.best.vision else ""
        converged = report.converged
        used_vision = report.used_vision
        final_verdict = report.final_verdict
        if args.open:
            open_file(out_path)
        if args.json:
            print(json.dumps({
                "report": report.model_dump(),
                "output": str(out_path),
                "preview": str(preview_path),
            }, indent=2))
            return 0
        console.print(f"[bold cyan]Pixelize refinement:[/] {len(report.rounds)} round(s), "
                      f"vision={'on' if used_vision else 'off (deterministic)'}"
                      f"{', converged early' if converged else ''}")
        table = Table(title="Refinement Rounds", show_header=True)
        table.add_column("Round")
        table.add_column("Best")
        table.add_column("Total", justify="right")
        table.add_column("Deterministic", justify="right")
        table.add_column("Vision", justify="right")
        table.add_column("Critique Applied")
        for r in report.rounds:
            rc = next((c for c in r.candidates if c.id == r.best_id), r.candidates[0] if r.candidates else None)
            table.add_row(
                str(r.index),
                r.best_id,
                f"{rc.total:.3f}" if rc else "-",
                f"{rc.det_score:.3f}" if rc else "-",
                f"{rc.vision_overall:.3f}" if rc and rc.vision else "-",
                ", ".join(r.suggestions_applied) or "-",
            )
        console.print(table)
    else:
        result = pixelize(args.image, args.palette, params)
        out_path = Path(args.out) if args.out else Path(args.image).with_name(Path(args.image).stem + ".pixel.png")
        result.image.save(out_path)
        preview_path = None
        preview_scale_used = args.preview_scale or _auto_preview_scale(result.image.size)
        if not args.no_preview:
            preview_path = out_path.with_name(out_path.stem + ".preview.png")
            result.image.resize(
                (result.image.width * preview_scale_used, result.image.height * preview_scale_used), Image.NEAREST
            ).save(preview_path)
        used_params = result.params
        det_metrics = {}
        vision_notes = ""
        converged = False
        used_vision = False
        final_verdict = None
        if args.open:
            open_file(out_path)
        if args.json:
            print(json.dumps({
                "output": str(out_path),
                "preview": str(preview_path) if preview_path else None,
                "params": used_params.model_dump(),
                "palette": result.palette_hex,
                "usage": [{"hex": h, "count": c} for h, c in result.usage],
                "source_size": list(result.source_size),
                "output_size": [result.image.width, result.image.height],
            }, indent=2))
            return 0

    console.print(f"[bold green]Saved:[/] {out_path}")
    if preview_path:
        console.print(f"Preview (x{preview_scale_used}): {preview_path}")
    console.print(f"Params: width={used_params.width} downscale={used_params.downscale} dither={used_params.dither}"
                  f" (strength {used_params.dither_strength}) pre_blur={used_params.pre_blur}"
                  f" sat={used_params.saturation:.2f} bright={used_params.brightness:.2f} contrast={used_params.contrast:.2f}")
    if det_metrics:
        console.print(f"Deterministic: score={det_metrics.get('score', 0):.3f} "
                      f"mean_dE={det_metrics.get('delta_mean', 0):.4f} coverage={det_metrics.get('coverage', 0):.2f}")
    if vision_notes:
        console.print(f"Vision notes: {escape(vision_notes)}")
    if final_verdict:
        console.print(f"Jev verdict: action=[bold yellow]{final_verdict.get('recommended_action', '-').upper()}[/] "
                      f"({final_verdict.get('production_ready_prob', 0) * 100:.0f}% ready)")
    return 0


def _load_identity(char_id: str | None):
    if not char_id:
        return None
    from .repo.catalog import AssetCatalog

    ident = AssetCatalog().get_identity(char_id)
    if ident is None:
        console.print(f"[yellow]warning:[/] character identity '{char_id}' not found; continuing without it")
    return ident


def _record_params(args: argparse.Namespace, command: str, keys: tuple[str, ...]) -> dict:
    params = {"command": command}
    for k in keys:
        v = getattr(args, k, None)
        if v not in (None, False, ""):
            params[k] = v
    return params


def _write_records(paths, gates: dict, params: dict, enabled: bool) -> list[str]:
    """Write <image>.gate.json beside each image; never fails the command."""
    if not enabled:
        return []
    from .gates import write_record

    written: list[str] = []
    for p in paths:
        try:
            written.append(str(write_record(p, gates, params=params)))
        except Exception as exc:  # noqa: BLE001 - a record is a convenience, the verdict stands
            console.print(f"[yellow]warning:[/] could not write gate record for {p}: {exc}")
    return written


def cmd_judge_still(args: argparse.Namespace) -> int:
    identity = _load_identity(getattr(args, "char_id", None))
    card = evaluate_still(
        image_or_path=args.image,
        palette_source=args.palette,
        scale=args.scale,
        verify_symmetry=args.symmetry,
        start_image=getattr(args, "start_image", False),
        target_height=getattr(args, "target_height", None),
        size_tier=getattr(args, "size_tier", None),
        margin_px=getattr(args, "margin", 3),
        facing=getattr(args, "facing", "south"),
        identity=identity,
    )

    verdict = None
    reference_path = getattr(args, "reference", None)
    if not reference_path and identity is not None and getattr(identity, "turnaround_stills", None):
        reference_path = identity.turnaround_stills.get("south")
        if reference_path and not Path(reference_path).is_file():
            console.print(f"[yellow]warning:[/] identity south still not found at {reference_path}; no reference comparison")
            reference_path = None
    if args.vision or reference_path:
        from PIL import Image
        img = Image.open(args.image).convert("RGBA")
        judge = VisionJudge(vision_model=args.vision_model)
    if args.vision:
        verdict = judge.orchestrate_judgment(
            still_scorecard=card,
            image=img,
            prompt_description=getattr(args, "desc", "") or "",
            expected_perspective=getattr(args, "perspective", "low top-down"),
        )
    if reference_path:
        from .judge import reference_gate

        ref_img = Image.open(reference_path).convert("RGBA")
        comparison = judge.compare_to_reference(
            img, ref_img,
            facing=getattr(args, "facing", None) if getattr(args, "facing", "south") != "south" else None,
            description=getattr(args, "desc", "") or "",
            expected_perspective=getattr(args, "perspective", "low top-down"),
        )
        ref_gate = reference_gate(comparison, facing=getattr(args, "facing", None))
        card.gates.append(ref_gate)
        card.passed = card.passed and ref_gate.passed
        if not ref_gate.passed:
            card.summary = f"{card.summary}; reference comparison failed" if card.passed is False else card.summary

    overall = verdict.passed if verdict else card.passed
    from .gates import gates_from_scorecard

    gates = gates_from_scorecard(card)
    if verdict:
        gates["vision"] = {"passed": bool(verdict.passed), "score": round(verdict.production_ready_prob, 3),
                           "summary": verdict.reasoning_summary}
    gates["still"] = {"passed": bool(overall)}
    records = _write_records(
        [args.image], gates,
        _record_params(args, "judge-still", ("palette", "scale", "symmetry", "start_image", "target_height",
                                              "size_tier", "margin", "facing", "char_id", "vision", "vision_model")),
        not getattr(args, "no_record", False),
    )

    if args.json:
        out_dict = card.model_dump()
        if verdict:
            out_dict["orchestrated_verdict"] = verdict.model_dump()
        out_dict["gate_records"] = records
        print(json.dumps(out_dict, indent=2))
        return 0 if overall else 1

    status_style = "bold green" if (verdict.passed if verdict else card.passed) else "bold red"
    status_text = "PASSED" if (verdict.passed if verdict else card.passed) else "FAILED"
    console.print(f"[{status_style}]Still Evaluation: {status_text}[/] - {card.path} (Score: {card.score:.2f})")
    console.print(f"Summary: {card.summary}\n")

    table = Table(title="Verification Gates", show_header=True)
    table.add_column("Gate", style="cyan")
    table.add_column("Status")
    table.add_column("Score", justify="right")
    table.add_column("Details")

    for g in card.gates:
        st = "[green]PASS[/]" if g.passed else "[red]FAIL[/]"
        details = g.errors[0] if g.errors else (g.warnings[0] if g.warnings else g.summary)
        table.add_row(g.name, st, f"{g.score:.2f}", escape(details))

    console.print(table)

    start_gate = card.gate("start_image")
    if start_gate:
        console.print("\n[bold]Start-image checks:[/]")
        for name, c in start_gate.details.get("checks", {}).items():
            mark = "[green]ok[/]" if c["passed"] else ("[dim]skip[/]" if c["passed"] is None else "[red]FAIL[/]")
            console.print(f" {mark:>14} {name}: {escape(str(c['detail']))}")
        for wmsg in start_gate.warnings:
            console.print(f"   [yellow]warning:[/] {escape(wmsg)}")

    if verdict:
        console.print("\n[bold magenta]Jev Orchestrated Verdict (Vision + Deterministic):[/]")
        console.print(f" • Readiness Probability: [bold]{verdict.production_ready_prob * 100:.0f}%[/] (confidence: {verdict.confidence * 100:.0f}%)")
        console.print(f" • Recommended Action: [bold yellow]{verdict.recommended_action.upper()}[/]")
        if verdict.vision_observation:
            obs = verdict.vision_observation
            console.print(f" • Vision Model ({obs.model_used}): elevation {obs.camera_elevation_degrees}° | match: {obs.perspective_match}")
            if obs.anomalies:
                console.print(f" • Anomalies: [red]{', '.join(obs.anomalies)}[/]")
            if obs.visual_smell_test_notes:
                console.print(f" • Visual Smell Test: {obs.visual_smell_test_notes}")

    for r in records:
        console.print(f"[dim]gate record: {r}[/]")
    return 0 if overall else 1


def cmd_judge_views(args: argparse.Namespace) -> int:
    from PIL import Image

    from .judge import check_direction_set, load_direction_frames

    identity = _load_identity(getattr(args, "char_id", None))
    try:
        frames = load_direction_frames(args.source, cell=args.cell)
    except Exception as e:  # noqa: BLE001 - reported as a CLI failure
        console.print(f"[bold red]Could not load views:[/] {e}")
        return 1
    anchor = Image.open(args.anchor).convert("RGBA") if args.anchor else None
    gate = check_direction_set(
        frames,
        anchor=anchor,
        identity=identity,
        palette=None if args.no_palette else args.palette,
        ground_tolerance=args.ground_tolerance,
        height_tolerance=args.height_tolerance,
        area_tolerance=args.area_tolerance,
    )

    from .gates import gates_from_scorecard
    from .judge.views import direction_frame_files

    src = Path(args.source)
    targets = list(direction_frame_files(src).values()) if src.is_dir() else [src]
    gates = gates_from_scorecard(gate)
    gates["views"] = {"passed": bool(gate.passed), "facings": list(frames)}
    records = _write_records(
        targets, gates,
        _record_params(args, "judge-views", ("cell", "anchor", "palette", "no_palette", "char_id",
                                              "ground_tolerance", "height_tolerance", "area_tolerance")),
        not getattr(args, "no_record", False),
    )

    if args.json:
        out_dict = gate.model_dump()
        out_dict["gate_records"] = records
        print(json.dumps(out_dict, indent=2))
        return 0 if gate.passed else 1

    status_style = "bold green" if gate.passed else "bold red"
    console.print(f"[{status_style}]Direction Set: {'PASSED' if gate.passed else 'FAILED'}[/] - {args.source} ({len(frames)} views, score {gate.score:.2f})")
    console.print(f"Summary: {escape(gate.summary)}\n")
    table = Table(title="Views vs anchor", show_header=True)
    table.add_column("Facing", style="cyan")
    table.add_column("Ground", justify="right")
    table.add_column("Height", justify="right")
    table.add_column("Opaque", justify="right")
    table.add_column("Parts", justify="right")
    anchor_m = gate.metrics.get("anchor", {})
    for facing, m in gate.metrics.get("views", {}).items():
        table.add_row(facing, str(m["ground_row"]), str(m["height"]), str(m["opaque"]), str(m["components"]))
    if anchor_m:
        table.add_row(f"[dim]anchor ({anchor_m.get('source')})[/]", str(anchor_m.get("ground_row")), str(anchor_m.get("height")), str(anchor_m.get("opaque")), str(anchor_m.get("components")))
    console.print(table)
    console.print("\n[bold]Checks:[/]")
    for name, c in gate.details.get("checks", {}).items():
        mark = "[green]ok[/]" if c["passed"] else ("[dim]skip[/]" if c["passed"] is None else "[red]FAIL[/]")
        console.print(f" {mark:>14} {name}: {escape(str(c['detail']))}")
    for wmsg in gate.warnings:
        console.print(f"   [yellow]warning:[/] {escape(wmsg)}")
    if records:
        console.print(f"[dim]gate records: {len(records)} written ({Path(records[0]).name}, ...)[/]")
    return 0 if gate.passed else 1


def cmd_judge_anim(args: argparse.Namespace) -> int:
    identity = _load_identity(getattr(args, "char_id", None))
    # The identity knows how the character moves; an explicit --locomotion wins.
    args.locomotion = args.locomotion or (identity.locomotion if identity is not None else None) or "legs"
    args.palette = args.palette or (identity.palette_id if identity is not None else None) or "luap-40"
    card = evaluate_animation(
        frames_or_sheet=args.path,
        cell=args.cell,
        is_walk=args.walk,
        palette_source=args.palette,
        locomotion=args.locomotion,
        source=getattr(args, "source", None),
    )

    from .gates import gates_from_scorecard

    src = Path(args.path)
    targets = sorted(f for f in src.iterdir() if f.suffix.lower() == ".png") if src.is_dir() else [src]
    records = _write_records(
        targets, gates_from_scorecard(card, "anim"),
        _record_params(args, "judge-anim", ("cell", "walk", "locomotion", "palette", "source", "char_id")),
        not getattr(args, "no_record", False),
    )

    if args.json:
        out_dict = card.model_dump()
        out_dict["gate_records"] = records
        print(json.dumps(out_dict, indent=2))
        return 0 if card.passed else 1

    status_style = "bold green" if card.passed else "bold red"
    status_text = "PASSED" if card.passed else "FAILED"
    console.print(f"[{status_style}]Animation Evaluation: {status_text}[/] - {card.frame_count} frames (Score: {card.score:.2f})")
    console.print(f"Summary: {card.summary}\n")

    table = Table(title="Animation Gates", show_header=True)
    table.add_column("Gate", style="cyan")
    table.add_column("Status")
    table.add_column("Score", justify="right")
    table.add_column("Details")

    for g in card.gates:
        st = "[green]PASS[/]" if g.passed else "[red]FAIL[/]"
        details = g.errors[0] if g.errors else (g.warnings[0] if g.warnings else g.summary)
        table.add_row(g.name, st, f"{g.score:.2f}", details)

    console.print(table)
    if card.suggested_actions:
        console.print("\n[yellow]Suggested Actions:[/]")
        for a in card.suggested_actions:
            console.print(f" • {a}")
    if records:
        console.print(f"[dim]gate records: {len(records)} written[/]")

    return 0 if card.passed else 1


def cmd_review_check(args: argparse.Namespace) -> int:
    from .gates import require_passed

    required = [g.strip() for g in args.require.split(",") if g.strip()]
    report = require_passed(args.paths, required)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    if report["ok"]:
        console.print(f"[bold green]Review check passed:[/] {report['checked']} file(s) carry a current passed "
                      f"{' / '.join(required)} record")
        return 0
    if not report["checked"]:
        console.print("[bold red]Review check failed:[/] no image files found")
        return 1
    console.print(f"[bold red]Review check failed:[/] {len(report['missing'])} of {report['checked']} file(s) lack a current passed "
                  f"{' / '.join(required)} record")
    for m in report["missing"]:
        have = f" (has: {', '.join(m['passed'])})" if m.get("passed") else ""
        console.print(f" • {m['path']}: {m['reason']}{have}")
    console.print("[dim]Gate them first: warden judge-still --start-image / judge-views / judge-anim[/]")
    return 1


def _pairs(items: list[str] | None, what: str) -> dict[str, str] | None:
    """``key=value`` options as a dict."""
    if not items:
        return None
    out: dict[str, str] = {}
    for item in items:
        key, eq, value = item.partition("=")
        if not eq or not key.strip():
            raise ValueError(f"{what} must be KEY=VALUE, got {item!r}")
        out[key.strip()] = value.strip()
    return out


def cmd_identity(args: argparse.Namespace) -> int:
    from .repo.identity import build_identity

    cat = AssetCatalog()
    if args.identity_command == "list":
        idents = cat.list_identities(game=args.game)
        if args.json:
            print(json.dumps([i.model_dump() for i in idents], indent=2))
            return 0
        table = Table(title="Character identities", show_header=True)
        for col in ("ID", "Name", "Game", "Cell", "Tier", "Locomotion", "Body", "Features"):
            table.add_column(col)
        for i in idents:
            table.add_row(i.id, i.name, i.game, "x".join(str(d) for d in i.dimensions), i.size_tier or "-",
                          i.locomotion or "-", i.body_class or "-", ", ".join(i.features) or "-")
        console.print(table)
        return 0

    if args.identity_command == "show":
        ident = cat.get_identity(args.id)
        if ident is None:
            console.print(f"[bold red]Unknown identity:[/] {args.id}")
            return 1
        print(json.dumps(ident.model_dump(), indent=2))
        return 0

    # set: create, or update the fields given
    try:
        dims = None
        if args.width or args.height:
            existing_dims = (cat.get_identity(args.id).dimensions if cat.get_identity(args.id) else [64, 64])
            dims = [args.width or existing_dims[0], args.height or existing_dims[1]]
        ident = build_identity(
            cat.get_identity(args.id),
            args.id,
            name=args.name,
            prompt_description=args.desc,
            game=args.game,
            palette_id=args.palette,
            perspective=args.perspective,
            dimensions=dims,
            ground_row_offset=args.ground_row_offset,
            prohibitions=args.prohibit,
            features=args.feature,
            turnaround_stills=_pairs(args.still, "--still"),
            style_anchors=_pairs(args.style_anchor, "--style-anchor"),
            locomotion=args.locomotion,
            body_class=args.body_class,
            size_tier=args.size_tier,
        )
    except ValueError as exc:
        console.print(f"[bold red]Identity not saved:[/] {escape(str(exc))}")
        return 1
    cat.save_identity(ident)
    if args.json:
        print(json.dumps(ident.model_dump(), indent=2))
        return 0
    console.print(f"[bold green]Identity saved:[/] {ident.id} ({ident.name}, {ident.game})")
    console.print(f"  cell {ident.dimensions[0]}x{ident.dimensions[1]}, ground row offset {ident.ground_row_offset}, "
                  f"tier {ident.size_tier or '-'}, locomotion {ident.locomotion or '-'}, body {ident.body_class or '-'}")
    for f in ident.features.values():
        console.print(f"  feature {f.name}: side {f.side or '-'}, marker {f.marker_hex or '-'}, box {f.relative_box or '-'}")
    for facing, path in ident.turnaround_stills.items():
        console.print(f"  still {facing}: {path}")
    for provider, value in ident.style_anchors.items():
        console.print(f"  style anchor {provider}: {value}")
    return 0


def cmd_repo_search(args: argparse.Namespace) -> int:
    cat = AssetCatalog()
    results = cat.search(
        query=args.query,
        game=args.game,
        character_id=args.character_id,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps({"count": len(results), "results": results}, indent=2))
        return 0

    console.print(f"[bold cyan]Asset Search Results ({len(results)} matches):[/]")
    table = Table(show_header=True)
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Category")
    table.add_column("Game")
    table.add_column("Perspective")
    table.add_column("Path")

    for r in results:
        table.add_row(r["id"], r["name"], r["category"], r["game"], r.get("perspective") or "-", r["file_path"])
    console.print(table)
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    cat = AssetCatalog()
    router = PipelineRouter(catalog=cat)
    char_ident = cat.get_identity(args.char_id) if args.char_id else None

    result = router.route_request(
        prompt=args.prompt,
        asset_type=args.type,
        character_identity=char_ident,
        game=args.game,
        perspective=args.perspective,
        size=args.size,
        is_animation=args.animation,
        is_vehicle_or_rigid=args.vehicle,
        force_fusion=args.fusion,
    )
    if args.json:
        print(result.model_dump_json(indent=2))
        return 0

    console.print(f"[bold cyan]Pipeline Routing: {result.action.upper()}[/]")
    console.print(f"Explanation: {result.explanation}\n")
    if result.action == "reuse_existing" and result.existing_asset_match:
        m = result.existing_asset_match
        console.print(f"[green]Matched Existing Asset:[/] {m['name']} -> {m['file_path']}")
    elif result.recipe:
        r = result.recipe
        console.print(f"[bold]Target Provider:[/] [yellow]{r.provider}[/] ({r.operation})")
        console.print(f"[bold]Sanitized Prompt:[/] {escape(r.prompt)}")
        console.print(f"[bold]Target Size:[/] {r.size}px (canvas {r.canvas_size}px)")
        console.print(f"[bold]Perspective:[/] {r.perspective} | Palette: {r.palette_id}")
        if r.rules:
            console.print("[bold]Playbook Rules:[/]")
            for rule in r.rules:
                console.print(f" • {rule}")
        if r.suggested_gates:
            console.print(f"[bold]Required Gates:[/] {', '.join(r.suggested_gates)}")

    return 0


def _slug(text: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or "asset"


def cmd_generate(args: argparse.Namespace) -> int:
    cat = AssetCatalog()
    router = PipelineRouter(catalog=cat)
    char_ident = cat.get_identity(args.char_id) if args.char_id else None

    result = router.route_request(
        prompt=args.prompt,
        asset_type=args.type,
        character_identity=char_ident,
        game=args.game,
        perspective=args.perspective,
        size=args.size,
        is_animation=args.animation,
        is_vehicle_or_rigid=args.vehicle,
        force_fusion=args.fusion,
    )

    if result.action == "reuse_existing" and result.existing_asset_match:
        m = result.existing_asset_match
        if args.json:
            print(result.model_dump_json(indent=2))
        else:
            console.print(f"[green]Reusing existing asset:[/] {m['name']} -> {m['file_path']}")
        return 0

    recipe = result.recipe
    if recipe is None:
        console.print("[bold red]No generation recipe produced.[/]")
        return 1
    if args.provider:
        recipe.provider = args.provider
    if getattr(args, "input", None):
        recipe.params["input_image"] = args.input

    out_dir = Path(args.out) if args.out else Path.cwd() / "spritewarden_generated" / _slug(args.prompt)
    output = dispatch(
        recipe, out_dir,
        force_ungated=getattr(args, "force_ungated", False),
        project_root=getattr(args, "project", None),
        register=not getattr(args, "no_register", False),
        catalog=cat,
        game=args.game,
        character_id=args.char_id,
    )

    if args.json:
        print(json.dumps({"recipe": recipe.model_dump(), "output": output.model_dump()}, indent=2))
        return 0 if output.success else 1

    status = "[bold green]OK[/]" if output.success else "[bold red]FAILED[/]"
    console.print(f"[bold cyan]Generate ({recipe.provider} / {recipe.operation}):[/] {status}")
    console.print(f"Prompt: {escape(recipe.prompt)}")
    if output.error:
        console.print(f"[red]Error:[/] {output.error}")
    for f in output.output_files:
        console.print(f" • {f}")
    if output.metadata:
        console.print(f"[dim]Metadata: {escape(json.dumps(output.metadata, default=str))}[/]")
    return 0 if output.success else 1


def cmd_pad(args: argparse.Namespace) -> int:
    from .sheet import pad_files

    try:
        report = pad_files(
            args.paths,
            out=args.out,
            size=args.size,
            canvas=args.canvas,
            grow=args.grow,
            align=args.align,
            margin=args.margin,
            power_of_two=args.power_of_two,
            fit=args.fit,
            crop_content=not args.no_crop,
            palette=args.palette,
            in_place=args.in_place,
            recursive=args.recursive,
        )
    except Exception as e:  # noqa: BLE001 - reported as a CLI failure
        console.print(f"[bold red]Pad failed:[/] {e}")
        return 1

    if args.json:
        print(json.dumps(report.model_dump(), indent=2))
        return 0

    console.print(
        f"[bold green]Padded {len(report.outputs)} image(s)[/] "
        f"-> {report.canvas[0]}x{report.canvas[1]} ({report.align})"
    )
    for o in report.outputs:
        console.print(f" • {o}")
    for w in report.warnings:
        console.print(f"[yellow]warning:[/] {w}")
    return 0


def cmd_sheet(args: argparse.Namespace) -> int:
    from .sheet import build_sheets

    try:
        reports = build_sheets(
            args.paths,
            args.out,
            group_by=args.group_by,
            palette=None if args.no_palette else args.palette,
            cell=args.cell,
            columns=args.columns,
            snap=not args.no_snap,
            engine=args.engine,
            align=args.align,
            margin=args.margin,
            power_of_two=args.power_of_two,
            crop=not args.no_crop,
            pixel_size=args.pixel_size,
            recursive=args.recursive,
        )
    except Exception as e:  # noqa: BLE001 - reported as a CLI failure
        console.print(f"[bold red]Sheet build failed:[/] {e}")
        return 1

    registered: dict[str, str] = {}
    if getattr(args, "register", False):
        from .repo.provenance import register_sheet

        cat = AssetCatalog()
        for r in reports:
            registered[r.sheet_path] = register_sheet(cat, r, game=args.game, character_id=getattr(args, "char_id", None))

    if args.json:
        out = [r.model_dump() for r in reports]
        for item in out:
            if item["sheet_path"] in registered:
                item["catalog_asset_id"] = registered[item["sheet_path"]]
        print(json.dumps(out, indent=2))
        return 0

    for path, asset_id in registered.items():
        console.print(f"[dim]registered in the catalog: {asset_id} -> {path}[/]")
    for r in reports:
        console.print(
            f"[bold green]Sheet:[/] {r.sheet_path} "
            f"({r.columns}x{r.rows}, cell {r.cell_size}px, {r.frame_count} frames, engine={r.engine})"
        )
        console.print(f"Manifest: {r.manifest_path}")
        for w in r.warnings:
            console.print(f"[yellow]warning:[/] {w}")
    return 0


def _recipe_tips(args: argparse.Namespace, reg) -> int:
    """`warden recipes --export-tips / --publish-tips / --mark-tips-exported`: the registry as tipshare tips."""
    from .learning.db import LearningDB
    from .learning.recipe_tips import TIPSHARE_CMD_ENV, build_recipe_tips, mark_exported, publish_tips

    tips = build_recipe_tips(topic=args.topic)
    if args.provider:
        canon = reg.canonical_provider(args.provider)
        tips = [t for t in tips if t["export_key"].split(":")[1] == canon]
    if args.export_tips:
        out_path = Path(args.export_tips)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(tips, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    db = LearningDB()
    if args.mark_tips_exported:
        count = mark_exported(tips, db)
        report = [{"title": t["title"], "export_key": t["export_key"], "action": "marked_exported"} for t in tips]
    else:
        try:
            report = publish_tips(tips, db, dry_run=not args.publish_tips, include_exported=args.all_tips)
        except ValueError as exc:
            console.print(f"[bold red]Not published:[/] {escape(str(exc))}")
            return 1
        count = sum(1 for r in report if r["action"] == "published")
    failed = any(r["action"] == "error" for r in report)
    if args.json:
        print(json.dumps({"tips": len(tips), "file": args.export_tips, "dry_run": not args.publish_tips,
                          "report": report}, indent=2))
        return 1 if failed else 0
    if args.export_tips:
        console.print(f"[bold green]{len(tips)} tip payload(s)[/] written to {args.export_tips} (topic {args.topic})")
    for r in report:
        extra = f" -> tip {r['tip_ref']}" if r.get("tip_ref") else (f": {r['error']}" if r.get("error") else "")
        console.print(f" • [cyan]{r['action']}[/] {escape(r['title'])}{escape(extra)}")
    if not args.publish_tips and not args.mark_tips_exported:
        console.print(f"[dim]Dry run: nothing was sent. --publish-tips publishes through the tipshare MCP server in "
                      f"{TIPSHARE_CMD_ENV} (searching first, skipping titles that exist); --mark-tips-exported records "
                      f"them as already published by hand.[/]")
    return 1 if failed else 0


def cmd_recipes(args: argparse.Namespace) -> int:
    from .router.recipes import registry, registry_json

    reg = registry()
    if args.provider and reg.canonical_provider(args.provider) is None:
        console.print(f"[bold red]Unknown provider:[/] {args.provider} (known: {', '.join(reg.providers)})")
        return 1
    if args.provider and args.operation and reg.get(args.provider, args.operation) is None:
        ops = ", ".join(r.operation for r in reg.select(args.provider))
        console.print(f"[bold red]Unknown operation:[/] {args.operation} (known for {args.provider}: {ops})")
        return 1
    if args.export_tips or args.publish_tips or args.mark_tips_exported:
        return _recipe_tips(args, reg)
    if args.json:
        print(registry_json(args.provider, args.operation))
        return 0

    if args.provider and args.operation:
        r = reg.get(args.provider, args.operation)
        assert r is not None
        console.print(f"[bold cyan]{r.provider} / {r.operation}[/]  {escape(r.description)}")
        if r.aliases:
            console.print(f"[dim]aliases: {', '.join(r.aliases)}[/]")
        if r.start_image and r.start_image.required:
            si = r.start_image
            console.print("[bold]Start image:[/]")
            for k, v in si.model_dump(exclude_none=True).items():
                if k == "notes":
                    continue
                console.print(f"  {k}: {escape(str(v))}")
            for n in si.notes:
                console.print(f"  [dim]- {escape(n)}[/]")
        elif r.start_image is None:
            console.print("[bold]Start image:[/] none (prompt / tool driven)")
        if r.prompt_rules:
            console.print("[bold]Prompt rules:[/]")
            for rule in r.prompt_rules:
                console.print(f"  - {escape(rule)}")
        pats = [p.pattern for p in reg.forbidden_patterns(r.provider, r.operation)]
        if pats:
            console.print(f"[bold]Forbidden words:[/] {escape(', '.join(pats))}")
        if r.arg_limits:
            console.print("[bold]Arg limits:[/]")
            for k, v in r.arg_limits.items():
                console.print(f"  {k}: {escape(str(v))}")
        console.print(f"[bold]Gates before:[/] {', '.join(r.required_gates_before) or '-'}")
        console.print(f"[bold]Gates after:[/] {', '.join(r.required_gates_after) or '-'}")
        if r.asset_id_params:
            console.print(f"[bold]Asset-id params:[/] {', '.join(r.asset_id_params)}")
        for n in r.notes:
            console.print(f"[dim]note: {escape(n)}[/]")
        for src in r.sources:
            console.print(f"[dim]source: {src}[/]")
        return 0

    table = Table(title="Provider recipes" + (f" ({args.provider})" if args.provider else ""), show_header=True)
    table.add_column("Provider", style="cyan")
    table.add_column("Operation")
    table.add_column("Start image")
    table.add_column("Gates before")
    table.add_column("Gates after")
    for r in reg.select(args.provider):
        si = r.start_image
        si_text = "-" if si is None else (
            ", ".join(b for b in [
                si.facing if si.facing and si.facing != "any" else "",
                si.canvas if si.canvas and si.canvas != "any" else "",
                f"<={si.max_px}px" if si.max_px else "",
                f"x{si.multiple_of}" if si.multiple_of else "",
            ] if b) or "any"
        ) + ("" if si is None or si.required else " (optional)")
        table.add_row(r.provider, r.operation, si_text, ", ".join(r.required_gates_before) or "-", ", ".join(r.required_gates_after) or "-")
    console.print(table)
    console.print("[dim]warden recipes <provider> <operation> for the full entry; --json for machine output[/]")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    try:
        proc = open_file(args.path)
        console.print(f"[bold green]Opened in Aseprite:[/] {args.path}")
        return 0
    except Exception as e:
        console.print(f"[bold red]Failed to open Aseprite:[/] {str(e)}")
        return 1


def cmd_edit(args: argparse.Namespace) -> int:
    def on_event(kind: str, msg: str):
        if kind == "opened":
            console.print(f"[bold green]{msg}[/]")
        elif kind == "saved":
            console.print(f"[bold cyan]Auto-exported:[/] {msg}")
        elif kind == "error":
            console.print(f"[bold red]Export issue:[/] {msg}")
        else:
            console.print(f"[dim]{msg}[/]")

    try:
        edit_sheet_interactive(
            sheet_path=args.sheet,
            cell=args.cell,
            palette_id=args.palette,
            timeout=args.timeout,
            callback=on_event,
        )
        return 0
    except Exception as e:
        console.print(f"[bold red]Edit session error:[/] {str(e)}")
        return 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="spritewarden", description="Intelligent Pixel Art Pipeline & QA System")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # judge still
    p_still = subparsers.add_parser("judge-still", help="Evaluate pixel art still image (grid, palette, clipping)")
    p_still.add_argument("image", help="Path to PNG image")
    p_still.add_argument("--palette", default="luap-40", help="Target palette name or file")
    p_still.add_argument("--scale", type=int, help="Expected scale factor (inferred if omitted)")
    p_still.add_argument("--symmetry", action="store_true", help="Check silhouette symmetry")
    p_still.add_argument("--start-image", action="store_true",
                         help="Readiness gate for an anchor/start image: square, margin, symmetry, height, strays, native pixels, palette")
    p_still.add_argument("--target-height", type=int, help="Expected drawn height in px (+-10%%) for --start-image")
    p_still.add_argument("--size-tier", choices=sorted(SIZE_TIERS), help="Resolution tier for --start-image (drawn height range)")
    p_still.add_argument("--margin", type=int, default=3, help="Required transparent margin in px for --start-image (default 3)")
    p_still.add_argument("--facing", default="south", help="Facing of the start image (south/north enable the symmetry check)")
    p_still.add_argument("--char-id", help="CharacterIdentity slug: supplies palette, cell size, feature markers and the south reference still")
    p_still.add_argument("--reference", help="Approved anchor image: vision compares candidate vs anchor side by side "
                                             "(parts added/removed/moved, hands, camera); offline = FAIL, never a silent accept")
    p_still.add_argument("--vision", action="store_true", help="Run multimodal vision model smell-test & Jev decision orchestration")
    p_still.add_argument("--vision-model", default=DEFAULT_VISION_MODEL, help=f"Vision model slug (default: {DEFAULT_VISION_MODEL})")
    p_still.add_argument("--desc", help="Expected visual description of character/object")
    p_still.add_argument("--perspective", default="low top-down", help="Expected perspective")
    p_still.add_argument("--no-record", action="store_true", help="Do not write the <image>.gate.json record")
    p_still.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    p_still.set_defaults(func=cmd_judge_still)

    # judge views (direction set)
    p_views = subparsers.add_parser("judge-views", help="Cross-view gate: compare the 4-8 facings of a direction set against an anchor")
    p_views.add_argument("source", help="Folder of <facing>.png files (south.png, down_right.png, ...) or a strip in game order")
    p_views.add_argument("--cell", type=int, help="Cell size when the source is a strip (default: strip height when it is N square cells)")
    p_views.add_argument("--anchor", help="Anchor image (default: the south view)")
    p_views.add_argument("--palette", default="luap-40", help="Target palette name or file")
    p_views.add_argument("--no-palette", action="store_true", help="Skip the palette check")
    p_views.add_argument("--char-id", help="CharacterIdentity slug: sided marker features enable the tool-side check")
    p_views.add_argument("--ground-tolerance", type=int, default=1, help="Allowed ground-row difference in px (default 1)")
    p_views.add_argument("--height-tolerance", type=int, default=2, help="Allowed drawn-height difference in px (default 2)")
    p_views.add_argument("--area-tolerance", type=float, default=0.20, help="Allowed opaque-area ratio difference (default 0.20)")
    p_views.add_argument("--no-record", action="store_true", help="Do not write <image>.gate.json records")
    p_views.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    p_views.set_defaults(func=cmd_judge_views)

    # judge anim
    p_anim = subparsers.add_parser("judge-anim", help="Evaluate animation (specks, spikes, walk cadence, loop wrap)")
    p_anim.add_argument("path", help="Path to spritesheet PNG or folder of frames")
    p_anim.add_argument("--cell", type=int, help="Cell dimension for spritesheet (e.g. 64 or 96)")
    p_anim.add_argument("--walk", action="store_true", help="Check walk mechanics (cadence for legs; steady turning base for wheels/treads)")
    p_anim.add_argument("--locomotion", choices=list(LOCOMOTION_MODES), default=None,
                        help="How the character moves: legs (alternating feet), wheels or treads (ground row + hub "
                             "steadiness). Default: the identity's locomotion (--char-id), else legs")
    p_anim.add_argument("--char-id", help="CharacterIdentity slug: supplies locomotion and palette when not given")
    p_anim.add_argument("--palette", default=None, help="Target palette name or file (default: the identity's, else luap-40)")
    p_anim.add_argument("--source", metavar="STILL.png",
                        help="The still the animation was made from: frame_integrity fails frames with enclosed holes "
                             "the still lacks (a keyed-out fill) or >15%% less opaque area")
    p_anim.add_argument("--no-record", action="store_true", help="Do not write <image>.gate.json records")
    p_anim.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    p_anim.set_defaults(func=cmd_judge_anim)

    # review-check (gatekeeper: refuse ungated images)
    p_rc = subparsers.add_parser("review-check", help="Exit 1 unless every image has a current passed gate record (<image>.gate.json)")
    p_rc.add_argument("paths", nargs="+", help="Image files, folders, or glob patterns")
    p_rc.add_argument("--require", default="still",
                      help="Gate name(s) that must have passed, comma-separated; any one satisfies (start_image, views, anim, still, ...)")
    p_rc.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    p_rc.set_defaults(func=cmd_review_check)

    # identity (character identities: what the judges and the router know about a character)
    p_ident = subparsers.add_parser("identity", help="Character identities: set / show / list")
    ident_sub = p_ident.add_subparsers(dest="identity_command", required=True)
    p_iset = ident_sub.add_parser("set", help="Create an identity, or update the fields given on an existing one")
    p_iset.add_argument("id", help="Unique slug, e.g. bildbot")
    p_iset.add_argument("--name", help="Display name (default: the id)")
    p_iset.add_argument("--desc", help="Core visual description (required when creating)")
    p_iset.add_argument("--game", help="Game slug (default: global)")
    p_iset.add_argument("--palette", help="Palette id (default: luap-40)")
    p_iset.add_argument("--perspective", help="Camera perspective (default: low top-down)")
    p_iset.add_argument("--width", type=int, help="Cell width in px")
    p_iset.add_argument("--height", type=int, help="Cell height in px")
    p_iset.add_argument("--ground-row-offset", type=int, help="Pixels up from the bottom cell edge where it stands")
    p_iset.add_argument("--prohibit", action="append", metavar="TEXT", help="Forbidden trait (repeatable; replaces the list)")
    p_iset.add_argument("--feature", action="append", metavar="NAME:SIDE:MARKER_HEX[:X,Y,W,H]",
                        help="Landmark feature (repeatable, merged by name): side left|right|- is the character's own "
                             "side, marker_hex the colour the judges locate, the box is on the front cell. "
                             "e.g. wrench:left:#ff00ff:10,30,8,12")
    p_iset.add_argument("--still", action="append", metavar="FACING=PATH", help="Approved turnaround still (repeatable), e.g. south=art/bot/south.png")
    p_iset.add_argument("--style-anchor", action="append", metavar="PROVIDER=VALUE", help="Provider id that pins the look (repeatable), e.g. spritecook=<asset_id>")
    p_iset.add_argument("--locomotion", choices=list(LOCOMOTION_MODES), help="How it moves (the walk gate's mode)")
    p_iset.add_argument("--body-class", choices=["figure", "rigid"], help="figure (head/torso/limbs) or rigid (vehicle, prop)")
    p_iset.add_argument("--size-tier", choices=sorted(SIZE_TIERS), help="Resolution tier of the whole set")
    p_iset.add_argument("--json", action="store_true", help="Output JSON")
    p_iset.set_defaults(func=cmd_identity)
    p_ishow = ident_sub.add_parser("show", help="Print one identity as JSON")
    p_ishow.add_argument("id")
    p_ishow.set_defaults(func=cmd_identity)
    p_ilist = ident_sub.add_parser("list", help="List identities")
    p_ilist.add_argument("--game", help="Filter by game")
    p_ilist.add_argument("--json", action="store_true", help="Output JSON")
    p_ilist.set_defaults(func=cmd_identity)

    # repo search
    p_search = subparsers.add_parser("search", help="Search central art repository")
    p_search.add_argument("query", nargs="?", default="", help="Search query")
    p_search.add_argument("--game", help="Filter by game")
    p_search.add_argument("--character-id", help="Filter by character slug")
    p_search.add_argument("--limit", type=int, default=15, help="Result limit")
    p_search.add_argument("--json", action="store_true", help="Output JSON")
    p_search.set_defaults(func=cmd_repo_search)

    # route
    p_route = subparsers.add_parser("route", help="Route generation request via Jev or Fusion")
    p_route.add_argument("prompt", help="Asset generation description")
    p_route.add_argument("--type", default="character", help="character, vehicle, tileset, props, icon")
    p_route.add_argument("--char-id", help="Target CharacterIdentity slug")
    p_route.add_argument("--game", default="global", help="Target game slug")
    p_route.add_argument("--perspective", default="low top-down", help="Camera perspective")
    p_route.add_argument("--size", type=int, default=64, help="Pixel size (16, 32, 64, 96)")
    p_route.add_argument("--animation", action="store_true", help="Is an animation cycle")
    p_route.add_argument("--vehicle", action="store_true", help="Is a vehicle / rigid object")
    p_route.add_argument("--fusion", action="store_true", help="Force OpenRouter Fusion multi-model deliberation")
    p_route.add_argument("--json", action="store_true", help="Output JSON")
    p_route.set_defaults(func=cmd_route)

    # generate (route + execute via provider driver)
    p_gen = subparsers.add_parser("generate", help="Route a request then execute it through the provider driver")
    p_gen.add_argument("prompt", help="Asset generation description")
    p_gen.add_argument("--type", default="character", help="character, vehicle, tileset, props, icon")
    p_gen.add_argument("--char-id", help="Target CharacterIdentity slug")
    p_gen.add_argument("--game", default="global", help="Target game slug")
    p_gen.add_argument("--perspective", default="low top-down", help="Camera perspective")
    p_gen.add_argument("--size", type=int, default=64, help="Pixel size (16, 32, 64, 96)")
    p_gen.add_argument("--animation", action="store_true", help="Is an animation cycle")
    p_gen.add_argument("--vehicle", action="store_true", help="Is a vehicle / rigid object")
    p_gen.add_argument("--fusion", action="store_true", help="Force OpenRouter Fusion multi-model deliberation")
    p_gen.add_argument("--provider", help=f"Override the routed provider (drivers: {', '.join(available_providers())})")
    p_gen.add_argument("--input", metavar="IMAGE",
                       help="The image a rotation/animation derives from (recipe params.input_image); it must carry a "
                            "passed gate record for the recipe's required_gates_before")
    p_gen.add_argument("--force-ungated", action="store_true",
                       help="Run even though the recipe's input image has no passed gate record (recorded in the output)")
    p_gen.add_argument("--project", help="Project root for the asset manifest / pipeline.json lookups (default: found from the cwd)")
    p_gen.add_argument("--no-register", action="store_true",
                       help="Do not register the outputs (provider, asset ids, params) in the asset catalog")
    p_gen.add_argument("--out", help="Output directory (default: ./spritewarden_generated/<slug>)")
    p_gen.add_argument("--json", action="store_true", help="Output JSON")
    p_gen.set_defaults(func=cmd_generate)

    # pad (normalize provider output onto a uniform canvas)
    p_pad = subparsers.add_parser("pad", help="Place sprite(s) on a uniform aligned canvas (fixed size or grow)")
    p_pad.add_argument("paths", nargs="+", help="Files, folders, or glob patterns")
    p_pad.add_argument("--size", type=int, help="Square canvas size in px (e.g. 64)")
    p_pad.add_argument("--canvas", help="WxH canvas (e.g. 96x64); overrides --size")
    p_pad.add_argument("--grow", type=int, help="Add N px of padding on every side of each frame's content")
    p_pad.add_argument("--align", default="bottom-center", help="top/center/bottom + left/center/right (default bottom-center)")
    p_pad.add_argument("--margin", type=int, default=0, help="Padding between content and canvas edge (px)")
    p_pad.add_argument("--power-of-two", action="store_true", help="Round the canvas up to a power of two")
    p_pad.add_argument("--fit", choices=["clip", "shrink"], default="clip",
                       help="Oversized content: clip at canvas edge or integer-downscale (NEAREST) to fit")
    p_pad.add_argument("--no-crop", action="store_true", help="Keep the existing canvas offsets instead of alpha-cropping first")
    p_pad.add_argument("--palette", help="Also snap to a palette (builtin, lospec slug, or file)")
    p_pad.add_argument("--out", help="Output PNG (single input) or directory (batch)")
    p_pad.add_argument("--in-place", action="store_true", help="Overwrite the input files")
    p_pad.add_argument("--recursive", action="store_true", help="Recurse into input folders")
    p_pad.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    p_pad.set_defaults(func=cmd_pad)

    # sheet builder
    p_sheet = subparsers.add_parser("sheet", help="Combine images/folders into a palette-snapped, evenly-framed spritesheet")
    p_sheet.add_argument("paths", nargs="+", help="Files, folders, or glob patterns")
    p_sheet.add_argument("--out", default="spritesheet.png", help="Output PNG (or directory when --group-by prefix)")
    p_sheet.add_argument("--palette", default="luap-40", help="Palette: builtin, lospec slug, or file")
    p_sheet.add_argument("--no-palette", action="store_true", help="Do not palette-snap (grid only)")
    p_sheet.add_argument("--cell", type=int, help="Fixed cell size in px (default: auto = max content)")
    p_sheet.add_argument("--columns", type=int, help="Grid columns (default: near-square)")
    p_sheet.add_argument("--align", default="bottom-center", help="top/center/bottom + left/center/right")
    p_sheet.add_argument("--margin", type=int, default=0, help="Padding inside each cell (px)")
    p_sheet.add_argument("--power-of-two", action="store_true", help="Round the cell size up to a power of two")
    p_sheet.add_argument("--no-crop", action="store_true", help="Keep full canvas instead of alpha-cropping each frame")
    p_sheet.add_argument("--recursive", action="store_true", help="Recurse into input folders")
    p_sheet.add_argument("--no-snap", action="store_true", help="Skip grid snapping; only palette-snap")
    p_sheet.add_argument("--engine", choices=["auto", "rust", "python", "none"], default="auto",
                         help="Grid-snap engine: rust (spritefusion-pixel-snapper) or python fallback")
    p_sheet.add_argument("--pixel-size", type=int, help="Override detected pixel block size")
    p_sheet.add_argument("--group-by", choices=["none", "prefix"], default="none",
                         help="Split into one sheet per filename prefix")
    p_sheet.add_argument("--register", action="store_true",
                         help="Register each composed sheet in the asset catalog (frames, cell, palette, sources)")
    p_sheet.add_argument("--game", default="global", help="Game slug for --register")
    p_sheet.add_argument("--char-id", help="CharacterIdentity slug for --register")
    p_sheet.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    p_sheet.set_defaults(func=cmd_sheet)

    # recipes (per-provider rules as data)
    p_rec = subparsers.add_parser("recipes", help="Print the per-provider recipe registry (start-image rules, limits, gates)")
    p_rec.add_argument("provider", nargs="?", help="pixellab, spritefusion, retrodiffusion, spritecook, openrouter_image")
    p_rec.add_argument("operation", nargs="?", help="Operation name or alias, e.g. direction-set, rotate, create_character_v3")
    p_rec.add_argument("--export-tips", metavar="OUT.json",
                       help="Write one tipshare tip payload per provider (title, summary, problem, solution, gotchas, "
                            "topic_slug), generated from the registry; reports a dry run of publishing them")
    p_rec.add_argument("--publish-tips", action="store_true",
                       help="Publish the tips through the tipshare MCP stdio server in SPRITEWARDEN_TIPSHARE_CMD "
                            "(searches first, skips titles that exist, records what was sent). Default is a dry run")
    p_rec.add_argument("--mark-tips-exported", action="store_true",
                       help="Record the tips as already published (by hand) without sending anything")
    p_rec.add_argument("--all-tips", action="store_true", help="Include tips already recorded as exported")
    p_rec.add_argument("--topic", default="godot-pixel-art", help="Tipshare topic slug for the tips")
    p_rec.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    p_rec.set_defaults(func=cmd_recipes)

    # open in aseprite
    p_open = subparsers.add_parser("open", help="Open an image or sprite in Aseprite for inspection")
    p_open.add_argument("path", help="Path to PNG or .aseprite file")
    p_open.set_defaults(func=cmd_open)

    # edit sheet roundtrip
    p_edit = subparsers.add_parser("edit", help="Interactive Aseprite sheet editing with auto-export on save")
    p_edit.add_argument("sheet", help="Path to PNG spritesheet")
    p_edit.add_argument("--cell", type=int, default=64, help="Cell dimension (default: 64)")
    p_edit.add_argument("--palette", default="luap-40", help="Palette swatches to load into Aseprite")
    p_edit.add_argument("--timeout", type=int, default=1800, help="Seconds to watch for saves")
    p_edit.set_defaults(func=cmd_edit)

    # pixelize
    p_pix = subparsers.add_parser("pixelize", help="Reduce any image to palette-snapped pixel art (lospec slugs supported)")
    p_pix.add_argument("image", help="Path to source image (any resolution)")
    p_pix.add_argument("--palette", default="luap-40", help="Palette: builtin name, lospec slug (e.g. 'na16'), lospec:<slug>, or file")
    p_pix.add_argument("--width", type=int, help="Target pixel width (height derived from aspect)")
    p_pix.add_argument("--height", type=int, help="Target pixel height")
    p_pix.add_argument("--scale", type=int, help="Divide source size by this factor (e.g. 8 -> 1/8 size)")
    p_pix.add_argument("--downscale", choices=["lanczos", "box", "nearest", "dominant"], default="lanczos",
                       help="Downscale method; 'dominant' snaps full-res then majority-votes per cell")
    p_pix.add_argument("--dither", choices=["none", "bayer2", "bayer4", "bayer8", "floyd", "sierra"], default="none")
    p_pix.add_argument("--strength", type=float, default=1.0, help="Dither strength (0..1.5)")
    p_pix.add_argument("--pre-blur", type=float, default=0.0, help="Gaussian blur radius before downscale")
    p_pix.add_argument("--saturation", type=float, default=1.0, help="Pre-snap saturation multiplier")
    p_pix.add_argument("--brightness", type=float, default=1.0, help="Pre-snap brightness multiplier")
    p_pix.add_argument("--contrast", type=float, default=1.0, help="Pre-snap contrast multiplier")
    p_pix.add_argument("--max-colors", type=int, help="Pre-quantize to K colors (k-means) before snapping")
    p_pix.add_argument("--no-guards", action="store_true", help="Disable grey/hue snap guards")
    p_pix.add_argument("--alpha", choices=["threshold", "keep", "opaque"], default="threshold",
                       help="Alpha handling: threshold 128, keep original, or flatten opaque")
    p_pix.add_argument("--out", help="Output PNG path (default <stem>.pixel.png next to source)")
    p_pix.add_argument("--preview-scale", type=int, help="Nearest-neighbor upscale factor for the preview PNG")
    p_pix.add_argument("--no-preview", action="store_true", help="Skip the preview PNG")
    p_pix.add_argument("--refine", action="store_true", help="Iteratively tune parameters (vision-guided when --vision)")
    p_pix.add_argument("--rounds", type=int, default=4, help="Max refinement rounds (default 4)")
    p_pix.add_argument("--session-dir", help="Refinement session directory (default <stem>_pixelize)")
    p_pix.add_argument("--vision", action="store_true", help="Judge candidates with the OpenRouter vision model (needs OPENROUTER_API_KEY)")
    p_pix.add_argument("--vision-model", default=DEFAULT_VISION_MODEL, help=f"Vision model slug (default: {DEFAULT_VISION_MODEL})")
    p_pix.add_argument("--desc", help="Subject description for the vision judge")
    p_pix.add_argument("--open", action="store_true", help="Open the result in Aseprite")
    p_pix.add_argument("--learn", action="store_true", help="Record the refine run as a learning episode")
    p_pix.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    p_pix.set_defaults(func=cmd_pixelize)

    # mcp server
    p_mcp = subparsers.add_parser("mcp", help="Start spritewarden Model Context Protocol (MCP) server")
    p_mcp.set_defaults(func=lambda _: run_mcp())

    # check-call / hook (enforcement core; normally answered by spritewarden.cli's fast path)
    from .enforce.cli import add_parsers as add_enforce_parsers

    add_enforce_parsers(subparsers)

    # learn (self-improvement knowledge base)
    learning_cli.register(subparsers)

    args = parser.parse_args(argv)
    sys.exit(args.func(args))


def run_mcp() -> int:
    from .server.mcp_server import run_mcp_server
    run_mcp_server()
    return 0


if __name__ == "__main__":
    main()
