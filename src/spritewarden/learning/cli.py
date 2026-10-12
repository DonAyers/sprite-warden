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
    from .curation import proposals_from_events

    db = LearningDB()
    if not db.list_sources():
        seed_sources(db)
        console.print("[dim]No sources registered; seeded the built-in provider set.[/]")
    events = check_all(db, provider=args.provider, source_id=args.source, limit=args.max)
    proposals = proposals_from_events(db)
    if args.json:
        _emit(args, {"checked": True, "events": [e.model_dump() for e in events],
                     "proposals": [p.model_dump() for p in proposals]})
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
    console.print(f"[yellow]Stored {len(events)} doc_events; {len(proposals)} new proposal(s). Review with `warden learn proposals`.[/]")
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


def cmd_reflect(args: argparse.Namespace) -> int:
    from .curation import derive

    db = LearningDB()
    proposals = derive(db, min_evidence=args.min_evidence, use_llm=args.llm)
    if args.json:
        _emit(args, {"created": [p.model_dump() for p in proposals]})
        return 0
    if not proposals:
        console.print("[dim]No new proposals (need enough evidence in a scope).[/]")
        return 0
    table = Table(title=f"New Proposals ({len(proposals)})")
    for col in ("ID", "Kind", "Target", "What"):
        table.add_column(col)
    for p in proposals:
        table.add_row(str(p.id), p.kind, p.target, escape(p.what))
    console.print(table)
    console.print("[yellow]Review with `warden learn proposals`, apply with `warden learn approve <id>`.[/]")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    from .proposals import review

    db = LearningDB()
    proposal = review(db, args.id, True)
    if not proposal:
        console.print(f"[red]Proposal {args.id} not found.[/]")
        return 1
    console.print(f"[bold green]Approved[/] proposal {args.id} ({proposal.kind} -> {proposal.target})")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    from .proposals import review

    db = LearningDB()
    proposal = review(db, args.id, False)
    if not proposal:
        console.print(f"[red]Proposal {args.id} not found.[/]")
        return 1
    console.print(f"[bold yellow]Rejected[/] proposal {args.id} ({proposal.kind})")
    return 0


def cmd_record_episode(args: argparse.Namespace) -> int:
    from .models import Episode
    from .taxonomy import normalize_perspective

    db = LearningDB()
    perspective = normalize_perspective(args.perspective).canonical or args.perspective
    episode = Episode(
        game=args.game,
        asset_type=args.asset_type,
        perspective=perspective,
        provider=args.provider,
        operation=args.operation,
        outcome=args.outcome,
        reason=args.reason or "",
        asset_id=args.asset_id,
    )
    eid = db.insert_episode(episode)
    if args.json:
        _emit(args, {"episode_id": eid})
        return 0
    console.print(f"[bold green]Recorded episode[/] {eid} ({args.provider}/{args.operation} -> {args.outcome})")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import report

    db = LearningDB()
    data = report(db)
    if args.json:
        _emit(args, data)
        return 0
    console.print(f"[bold cyan]Learning DB:[/] {data['db_path']}  (llm_enabled={data['llm_enabled']})")
    console.print(
        f"Sources: {data['sources']['total']} | Events: {data['events']['total']} "
        f"({data['events']['significant_new']} significant new) | Episodes: {data['episodes']['total']} | "
        f"Strategies: {data['strategies']['total']} | Open proposals: {data['proposals']['open']}"
    )
    console.print(
        f"Budget: ${data['budget']['spend_usd']:.2f}/${data['budget']['cap_usd']:.2f} "
        f"({data['budget']['calls']} calls) this {data['budget']['period']}"
    )
    if data["warnings"]:
        console.print("[yellow]Warnings:[/]")
        for w in data["warnings"]:
            console.print(f" • {w}")
    else:
        console.print("[green]No warnings.[/]")
    return 0


def cmd_export_tips(args: argparse.Namespace) -> int:
    from .tipshare_bridge import export_tips

    if args.mark:
        console.print("[red]Blind marking disabled: publish with a verified get_tip receipt.[/]")
        return 1
    db = LearningDB()
    payloads = export_tips(db, topic=args.topic, mark=args.mark and not args.publish, include_exported=args.all)
    if getattr(args, "recipes", False):
        from .recipe_tips import build_recipe_tips

        payloads = payloads + build_recipe_tips(topic=args.topic)
    if getattr(args, "publish", False) or getattr(args, "dry_run", False):
        # Through the tipshare MCP stdio server: search first, skip existing titles, record what was sent.
        from .recipe_tips import publish_tips

        try:
            report = publish_tips(payloads, db, dry_run=not args.publish, include_exported=args.all)
        except ValueError as exc:
            console.print(f"[bold red]Not published:[/] {escape(str(exc))}")
            return 1
        failed = any(r["action"] == "error" for r in report)
        if args.json:
            _emit(args, {"count": len(payloads), "dry_run": not args.publish, "report": report})
            return 1 if failed else 0
        for r in report:
            extra = f" -> tip {r['tip_ref']}" if r.get("tip_ref") else (f": {r['error']}" if r.get("error") else "")
            console.print(f" • [cyan]{r['action']}[/] {escape(r['title'])}{escape(extra)}")
        if not args.publish:
            console.print("[dim]Dry run: nothing was sent (--publish sends).[/]")
        return 1 if failed else 0
    if args.json:
        _emit(args, {"count": len(payloads), "tips": payloads})
        return 0
    if not payloads:
        console.print("[dim]No unexported active strategies.[/]")
        return 0
    console.print(f"[bold green]{len(payloads)} tip payload(s)[/] ready for `share_tip` (topic {args.topic}):")
    for t in payloads:
        console.print(f" • {escape(t['title'])}")
    if not args.mark:
        console.print("[dim]Use --publish for a verified export receipt; blind --mark is disabled.[/]")
    return 0


def cmd_watch_project(args: argparse.Namespace) -> int:
    from .project_watch import register_project

    db = LearningDB()
    count = register_project(db, provider=args.provider, root=args.root, patterns=args.pattern or None)
    if args.json:
        _emit(args, {"registered": count})
        return 0
    console.print(f"[bold green]Registered {count} project files[/] for provider '{args.provider}'.")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    from .scheduler import LearningScheduler

    scheduler = LearningScheduler()
    if args.once:
        events = scheduler.run_once()
        if args.json:
            _emit(args, {"events": [e.model_dump() for e in events]})
            return 0
        console.print(f"[green]Checked due sources; {len(events)} change(s).[/]")
        return 0
    console.print("[dim]Watching provider docs in the background. Ctrl+C to stop.[/]")
    try:
        scheduler.start(daemon=False)
        while scheduler.is_running():
            import time

            time.sleep(0.5)
    except KeyboardInterrupt:
        scheduler.stop()
        console.print("\n[dim]Stopped.[/]")
    return 0


def cmd_knowledge_sync(args):
    from pathlib import Path
    from .tipshare_bridge import sync_outcomes, import_corrections, curated_corrections
    # Dry-runs migrate a disposable snapshot, never the configured/live DB.
    import os
    import sqlite3
    import tempfile
    from .db import DEFAULT_DB_PATH
    temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) if not args.store else None
    if temporary:
        source = Path(os.environ.get("SPRITEWARDEN_LEARNING_DB", str(DEFAULT_DB_PATH)))
        target = Path(temporary.name) / "preview.db"
        if source.exists():
            src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                src.close(); dst.close()
        db = LearningDB(target)
    else:
        db = LearningDB()
    data = json.loads(Path(args.input).read_text(encoding="utf-8")) if args.input else None
    dry_run = not args.store
    if args.learn_command == "sync-outcomes":
        result = sync_outcomes(db, data, dry_run=dry_run)
    else:
        corrections = curated_corrections() if args.curated else data
        if corrections is None:
            raise ValueError("Provide --curated or --input corrections.json")
        result = import_corrections(db, corrections, dry_run=dry_run)
    _emit(args, result)
    if temporary:
        temporary.cleanup()
    return 0


def cmd_watch_dependencies(args):
    from .registry import register_dependencies
    count = register_dependencies(LearningDB(), args.provider, operation_docs=args.operation_doc or [], skill_paths=args.skill or [])
    _emit(args, {"registered": count})
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "learn",
        help="Self-learning knowledge base: watch provider docs, record episodes, review proposals",
    )
    learn = p.add_subparsers(dest="learn_command", required=True)

    for name in ("sync-outcomes", "corrections"):
        sync = learn.add_parser(name, help="Review-only knowledge ingestion; defaults to dry-run")
        sync.add_argument("--input", help="Tipshare export or correction proposal JSON")
        mode = sync.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--store", action="store_true", help="Store local review proposals only; never publish/apply policy")
        sync.add_argument("--curated", action="store_true", help="Use audited #34/#31/#40/#60/#63 proposals")
        sync.add_argument("--json", action="store_true")
        sync.set_defaults(func=cmd_knowledge_sync)
    deps = learn.add_parser("watch-dependencies", help="Watch explicit operation docs and installed skill files")
    deps.add_argument("--provider", required=True)
    deps.add_argument("--operation-doc", action="append")
    deps.add_argument("--skill", action="append")
    deps.add_argument("--json", action="store_true")
    deps.set_defaults(func=cmd_watch_dependencies)

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

    pr = learn.add_parser("reflect", help="Derive strategy proposals from recorded episodes")
    pr.add_argument("--min-evidence", type=int, default=3, help="Minimum episodes per scope (default 3)")
    pr.add_argument("--llm", action="store_true", help="Use the capped LLM reflection (default: deterministic)")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_reflect)

    pa = learn.add_parser("approve", help="Approve a proposal and apply it to the registry")
    pa.add_argument("id", type=int)
    pa.add_argument("--json", action="store_true")
    pa.set_defaults(func=cmd_approve)

    pj = learn.add_parser("reject", help="Reject a proposal")
    pj.add_argument("id", type=int)
    pj.add_argument("--json", action="store_true")
    pj.set_defaults(func=cmd_reject)

    pre = learn.add_parser("record-episode", help="Record a generation/judge attempt")
    pre.add_argument("--provider", required=True)
    pre.add_argument("--operation", required=True)
    pre.add_argument("--outcome", required=True, choices=["accepted", "rejected", "rerolled", "pending"])
    pre.add_argument("--asset-type", default="sprite")
    pre.add_argument("--perspective", default="unknown")
    pre.add_argument("--game", default="global")
    pre.add_argument("--reason", default="")
    pre.add_argument("--asset-id")
    pre.add_argument("--json", action="store_true")
    pre.set_defaults(func=cmd_record_episode)

    pd = learn.add_parser("doctor", help="Report knowledge-base health and staleness")
    pd.add_argument("--json", action="store_true")
    pd.set_defaults(func=cmd_doctor)

    pt = learn.add_parser("export-tips", help="Emit tipshare `share_tip` payloads for active strategies")
    pt.add_argument("--topic", default="godot-pixel-art")
    pt.add_argument("--mark", action="store_true", help="Deprecated: refuses blind marking; use verified publishing")
    pt.add_argument("--all", action="store_true", help="Include already-exported strategies")
    pt.add_argument("--recipes", action="store_true", help="Also include one tip per provider generated from the recipe registry")
    pt.add_argument("--publish", action="store_true",
                    help="Publish through the tipshare MCP stdio server in SPRITEWARDEN_TIPSHARE_CMD (searches first, "
                         "skips titles that exist, records what was sent in tip_exports)")
    pt.add_argument("--dry-run", action="store_true", help="Report what --publish would do without sending anything")
    pt.add_argument("--json", action="store_true")
    pt.set_defaults(func=cmd_export_tips)

    pw = learn.add_parser("watch-project", help="Register a project's palette/config files as watched sources")
    pw.add_argument("root", help="Project root directory")
    pw.add_argument("--provider", required=True, help="Project slug used as provider")
    pw.add_argument("--pattern", action="append", help="Glob pattern (repeatable)")
    pw.add_argument("--json", action="store_true")
    pw.set_defaults(func=cmd_watch_project)

    pwa = learn.add_parser("watch", help="Run the background doc watcher (or --once)")
    pwa.add_argument("--once", action="store_true", help="Check due sources once and exit")
    pwa.add_argument("--json", action="store_true")
    pwa.set_defaults(func=cmd_watch)
