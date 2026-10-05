"""Acquisition: conditional-GET fetching, extraction, snapshotting, and event creation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .db import LearningDB
from .diffing import analyze_change, content_hash, deterministic_summary
from .models import DocEvent, Snapshot, Source, utcnow

FetchFn = Callable[..., "FetchResult"]


@dataclass
class FetchResult:
    url: str
    status: int
    body: str = ""
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    error: str | None = None


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def extract_body(body: str, kind: str) -> str:
    """Normalize a fetched body for stable diffing (pretty JSON, stripped HTML, raw md)."""
    if kind in ("json_api", "openapi"):
        try:
            obj = json.loads(body)
            return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
        except Exception:
            return body
    if kind == "html":
        return _strip_html(body)
    return body


def fetch(
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout: float = 15.0,
    fetch_fn: FetchFn | None = None,
) -> FetchResult:
    """Conditional GET, or read a file:// URL. fetch_fn is injectable for tests."""
    if fetch_fn is not None:
        return fetch_fn(url, etag=etag, last_modified=last_modified, timeout=timeout)
    if url.startswith("file://") or ("://" not in url and Path(url).is_file()):
        try:
            if url.startswith("file://"):
                from urllib.parse import urlparse
                from urllib.request import url2pathname

                path = Path(url2pathname(urlparse(url).path))
            else:
                path = Path(url)
            return FetchResult(url=url, status=200, body=path.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            return FetchResult(url=url, status=0, error=str(exc))
    try:
        import httpx

        headers = {"User-Agent": "spritewarden-learning (+doc watch)"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 304:
                return FetchResult(url=url, status=304, not_modified=True)
            return FetchResult(
                url=url,
                status=resp.status_code,
                body=resp.text,
                etag=resp.headers.get("ETag"),
                last_modified=resp.headers.get("Last-Modified"),
            )
    except Exception as exc:
        return FetchResult(url=url, status=0, error=str(exc))


def check_source(
    source: Source,
    db: LearningDB,
    fetch_fn: FetchFn | None = None,
) -> list[DocEvent]:
    """Fetch one source, snapshot it, and return any detected DocEvents."""
    if not source.enabled:
        return []
    if source.kind == "mcp_tools":
        return []

    result = fetch(source.url, etag=source.etag, last_modified=source.last_modified, fetch_fn=fetch_fn)
    now = utcnow()

    if result.error:
        return []
    if result.not_modified:
        db.update_source_fetch_state(source.id, source.etag, source.last_modified, source.last_hash, now)
        return []

    extracted = extract_body(result.body, source.kind)
    new_hash = content_hash(extracted)
    if new_hash == source.last_hash:
        db.update_source_fetch_state(source.id, result.etag, result.last_modified, new_hash, now)
        return []

    prev = db.latest_snapshot(source.id)
    db.insert_snapshot(
        Snapshot(
            source_id=source.id,
            fetched_at=now,
            http_status=result.status,
            content_hash=new_hash,
            body=extracted,
            extracted={"length": len(extracted)},
        )
    )

    old_body = prev.body if prev else ""
    is_json = source.kind in ("json_api", "openapi")
    old_json = new_json = None
    if is_json:
        old_json, new_json = _safe_json(old_body), _safe_json(extracted)

    record = analyze_change(old_body, extracted, old_json, new_json, name=source.id)
    events: list[DocEvent] = []
    if record:
        what, how, why = deterministic_summary(record, source)
        event = DocEvent(
            source_id=source.id,
            provider=source.provider,
            detected_at=now,
            change_kind=record["change_kind"],
            significance=record["significance"],
            signals=record["signals"],
            diff_text=record["diff_text"][:20000],
            summary_what=what,
            summary_how=how,
            summary_why=why,
        )
        event.id = db.insert_event(event)
        events.append(event)

    db.update_source_fetch_state(source.id, result.etag, result.last_modified, new_hash, now)
    return events


def _safe_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def check_all(
    db: LearningDB,
    provider: str | None = None,
    source_id: str | None = None,
    fetch_fn: FetchFn | None = None,
    limit: int | None = None,
) -> list[DocEvent]:
    """Check enabled sources (optionally filtered) and collect all detected events."""
    sources = db.list_sources(provider=provider, enabled_only=True)
    if source_id:
        sources = [s for s in sources if s.id == source_id]
    if limit is not None:
        sources = sources[:limit]
    events: list[DocEvent] = []
    for source in sources:
        events.extend(check_source(source, db, fetch_fn=fetch_fn))
    return events
