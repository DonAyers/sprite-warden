"""Command-Line Interface for slaygent: pixel art QA judges, repository, and routing."""

import argparse
import json
import sys
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.markup import escape

from .judge import evaluate_still, evaluate_animation, VisionJudge
from .judge.vision import DEFAULT_VISION_MODEL
from .tools.aseprite import open_file, edit_sheet_interactive, find_aseprite
from .repo.catalog import AssetCatalog
from .repo.identity import CharacterIdentity
from .repo.palette import registry as palettes
from .router import PipelineRouter

console = Console()


def cmd_judge_still(args: argparse.Namespace) -> int:
    card = evaluate_still(
        image_or_path=args.image,
        palette_source=args.palette,
        scale=args.scale,
        verify_symmetry=args.symmetry,
    )

    verdict = None
    if args.vision:
        from PIL import Image
        img = Image.open(args.image).convert("RGBA")
        judge = VisionJudge(vision_model=args.vision_model)
        verdict = judge.orchestrate_judgment(
            still_scorecard=card,
            image=img,
            prompt_description=getattr(args, "desc", "") or "",
            expected_perspective=getattr(args, "perspective", "low top-down"),
        )

    if args.json:
        out_dict = card.model_dump()
        if verdict:
            out_dict["orchestrated_verdict"] = verdict.model_dump()
        print(json.dumps(out_dict, indent=2))
        return 0 if (verdict.passed if verdict else card.passed) else 1

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
        table.add_row(g.name, st, f"{g.score:.2f}", details)

    console.print(table)

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

    return 0 if (verdict.passed if verdict else card.passed) else 1


def cmd_judge_anim(args: argparse.Namespace) -> int:
    card = evaluate_animation(
        frames_or_sheet=args.path,
        cell=args.cell,
        is_walk=args.walk,
        palette_source=args.palette,
    )
    if args.json:
        print(card.model_dump_json(indent=2))
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

    return 0 if card.passed else 1


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


def main() -> None:
    parser = argparse.ArgumentParser(prog="spritewarden", description="Intelligent Pixel Art Pipeline & QA System")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # judge still
    p_still = subparsers.add_parser("judge-still", help="Evaluate pixel art still image (grid, palette, clipping)")
    p_still.add_argument("image", help="Path to PNG image")
    p_still.add_argument("--palette", default="luap-40", help="Target palette name or file")
    p_still.add_argument("--scale", type=int, help="Expected scale factor (inferred if omitted)")
    p_still.add_argument("--symmetry", action="store_true", help="Check silhouette symmetry")
    p_still.add_argument("--vision", action="store_true", help="Run multimodal vision model smell-test & Jev decision orchestration")
    p_still.add_argument("--vision-model", default=DEFAULT_VISION_MODEL, help=f"Vision model slug (default: {DEFAULT_VISION_MODEL})")
    p_still.add_argument("--desc", help="Expected visual description of character/object")
    p_still.add_argument("--perspective", default="low top-down", help="Expected perspective")
    p_still.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    p_still.set_defaults(func=cmd_judge_still)

    # judge anim
    p_anim = subparsers.add_parser("judge-anim", help="Evaluate animation (specks, spikes, walk cadence, loop wrap)")
    p_anim.add_argument("path", help="Path to spritesheet PNG or folder of frames")
    p_anim.add_argument("--cell", type=int, help="Cell dimension for spritesheet (e.g. 64 or 96)")
    p_anim.add_argument("--walk", action="store_true", help="Check alternating foot walk cadence")
    p_anim.add_argument("--palette", default="luap-40", help="Target palette name or file")
    p_anim.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    p_anim.set_defaults(func=cmd_judge_anim)

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

    # mcp server
    p_mcp = subparsers.add_parser("mcp", help="Start spritewarden Model Context Protocol (MCP) server")
    p_mcp.set_defaults(func=lambda _: run_mcp())

    args = parser.parse_args()
    sys.exit(args.func(args))


def run_mcp() -> int:
    from .server.mcp_server import run_mcp_server
    run_mcp_server()
    return 0


if __name__ == "__main__":
    main()
