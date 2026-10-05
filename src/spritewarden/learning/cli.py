"""CLI surface for the learning subsystem (`warden learn ...`)."""

from __future__ import annotations

import argparse
import json

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .acquisition import check_all
from .db import LearningDB
from .registry import seed_sources

console = Console()


def _emit(args: argparse.Namespace, payload: dict) -> None:
    print(json.dumps(payload, indent=2, default=str))


def cmd_sources(args: argparse.Namespace) -> int:
    db = LearningDB()
    sources = db.list_sources(provider=args.provider)
    if args.json:
        _emit(args, {"sources": [s.model_dump() for s in sources]})
        return 0
    table = Table(title=f"Learned Sources ({len(sources)})")
    table.add_column("ID", style="cyan")
    table.add_column("Provider")
    table.add_column("Kind")
    table.add_column("Cadence")
    table.add_column("Last Checked")
    table.add_column("URL")
    for s in sources:
        table.add_row(s.id, s.provider, s.kind, f"{s.cadence_s}s", s.last_checked or "-", escape(s.url))
    console.print(table)
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    db = LearningDB()
    count = seed_sources(db)
    if args.json:
        _emit(args, {"seeded": count})
        return 0
    console.print(f"[bold green]Seeded {count} sources.[/]")
    return 0


def cmd_add_source(args: argparse.Namespace) -> int:
    from .models import Source

    db = LearningDB()
    db.upsert_source(
        Source(
            id=args.id,
            provider=args.provider,
            kind=args.kind,
            url=args.url,
            cadence_s=args.cadence,
            notes=args.notes or "",
        )
    )
    if args.json:
        _emit(args, {"added": args.id})
        return 0
    console.print(f"[bold green]Registered source[/] {args.id} ({args.provider}/{args.kind})")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    db = LearningDB()
    if not db.list_sources():
        seed_sources(db)
        console.print("[dim]No sources registered; seeded the built-in provider set.[/]")
    events = check_all(db, provider=args.provider, source_id=args.source, limit=args.max)
    if args.json:
        _emit(args, {"checked": True, "events": [e.model_dump() for e in events]})
        return 0
    if not events:
        console.print("[bold green]No changes detected.[/]")
        return 0
    table = Table(title=f"Detected Changes ({len(events)})")
    table.add_column("ID", style="cyan")
    table.add_column("Source")
    table.add_column("Kind")
    table.add_column("Sig", justify="right")
    table.add_column("Signals")
    table.add_column("What")
    for e in events:
        table.add_row(str(e.id), e.source_id, e.change_kind, f"{e.significance:.2f}", ", ".join(e.signals) or "-", escape(e.summary_what))
    console.print(table)
    console.print(f"[yellow]Stored {len(events)} doc_events. Review with `warden learn events`.[/]")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    db = LearningDB()
    events = db.list_events(limit=args.limit, source_id=args.source, review_state=args.state)
    if args.json:
        _emit(args, {"events": [e.model_dump() for e in events]})
        return 0
    table = Table(title=f"Doc Events ({len(events)})")
    table.add_column("ID", style="cyan")
    table.add_column("Source")
    table.add_column("Kind")
    table.add_column("Sig", justify="right")
    table.add_column("Model")
    table.add_column("What")
    for e in events:
        table.add_row(str(e.id), e.source_id, e.change_kind, f"{e.significance:.2f}", e.model, escape(e.summary_what))
    console.print(table)
    return 0


def cmd_episodes(args: argparse.Namespace) -> int:
    db = LearningDB()
    episodes = db.list_episodes(limit=args.limit, game=args.game, provider=args.provider, outcome=args.outcome)
    if args.json:
        _emit(args, {"episodes": [e.model_dump() for e in episodes]})
        return 0
    table = Table(title=f"Episodes ({len(episodes)})")
    for col in ("ID", "Provider", "Operation", "Asset", "Perspective", "Outcome", "Reason"):
        table.add_column(col, style="cyan" if col == "ID" else None)
    for e in episodes:
        table.add_row(str(e.id), e.provider, e.operation, e.asset_type, e.perspective, e.outcome, escape(e.reason))
    console.print(table)
    return 0


def cmd_strategy(args: argparse.Namespace) -> int:
    db = LearningDB()
    if args.query:
        strategies = db.search_strategies(args.query, limit=args.limit)
    else:
        strategies = db.list_strategies(scope_key=args.scope, status=args.status)[: args.limit]
    if args.json:
        _emit(args, {"strategies": [s.model_dump() for s in strategies]})
        return 0
    table = Table(title=f"Strategies ({len(strategies)})")
    for col in ("ID", "Scope", "Status", "Conf", "What"):
        table.add_column(col)
    for s in strategies:
        table.add_row(s.id, s.scope_key, s.status, f"{s.confidence:.2f}", escape(s.what))
    console.print(table)
    return 0


def cmd_proposals(args: argparse.Namespace) -> int:
    db = LearningDB()
    proposals = db.list_proposals(status=args.status, limit=args.limit)
    if args.json:
        _emit(args, {"proposals": [p.model_dump() for p in proposals]})
        return 0
    table = Table(title=f"Proposals ({len(proposals)})")
    for col in ("ID", "Kind", "Target", "What"):
        table.add_column(col)
    for p in proposals:
        table.add_row(str(p.id), p.kind, p.target, escape(p.what))
    console.print(table)
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "learn",
        help="Self-learning knowledge base: watch provider docs, record episodes, review proposals",
    )
    learn = p.add_subparsers(dest="learn_command", required=True)

    ps = learn.add_parser("sources", help="List watched provider sources")
    ps.add_argument("--provider", help="Filter by provider")
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=cmd_sources)

    pas = learn.add_parser("add-source", help="Register a watched source (doc/API/tool surface)")
    pas.add_argument("id", help="Unique source id")
    pas.add_argument("--provider", required=True)
    pas.add_argument("--kind", required=True,
                     choices=["llms_txt", "md", "openapi", "mcp_tools", "rss", "changelog", "html", "json_api", "file"])
    pas.add_argument("--url", required=True)
    pas.add_argument("--cadence", type=int, default=86400, help="Seconds between checks")
    pas.add_argument("--notes", default="")
    pas.add_argument("--json", action="store_true")
    pas.set_defaults(func=cmd_add_source)

    pseed = learn.add_parser("seed", help="Register the built-in provider source set")
    pseed.add_argument("--json", action="store_true")
    pseed.set_defaults(func=cmd_seed)

    pc = learn.add_parser("check", help="Fetch + diff watched sources, recording doc_events")
    pc.add_argument("--provider", help="Only check this provider")
    pc.add_argument("--source", help="Only check this source id")
    pc.add_argument("--max", type=int, help="Limit number of sources checked")
    pc.add_argument("--json", action="store_true")
    pc.set_defaults(func=cmd_check)

    pe = learn.add_parser("events", help="List detected documentation/tool changes")
    pe.add_argument("--source", help="Filter by source id")
    pe.add_argument("--state", help="Filter by review state (new/reviewed/acknowledged/dismissed)")
    pe.add_argument("--limit", type=int, default=30)
    pe.add_argument("--json", action="store_true")
    pe.set_defaults(func=cmd_events)

    pep = learn.add_parser("episodes", help="List recorded generation/judge episodes")
    pep.add_argument("--game")
    pep.add_argument("--provider")
    pep.add_argument("--outcome")
    pep.add_argument("--limit", type=int, default=30)
    pep.add_argument("--json", action="store_true")
    pep.set_defaults(func=cmd_episodes)

    pst = learn.add_parser("strategy", help="List or search distilled strategies")
    pst.add_argument("--scope", help="Filter by scope key")
    pst.add_argument("--status", help="Filter by status (proposed/active/deprecated/superseded)")
    pst.add_argument("--query", help="FTS search across strategies")
    pst.add_argument("--limit", type=int, default=30)
    pst.add_argument("--json", action="store_true")
    pst.set_defaults(func=cmd_strategy)

    pp = learn.add_parser("proposals", help="List suggested changes awaiting review")
    pp.add_argument("--status", default="open", help="open/approved/rejected (default: open)")
    pp.add_argument("--limit", type=int, default=30)
    pp.add_argument("--json", action="store_true")
    pp.set_defaults(func=cmd_proposals)
