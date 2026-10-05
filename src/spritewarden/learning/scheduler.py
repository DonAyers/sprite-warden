"""Bounded background scheduler: cadence-aware source checking, deterministic and offline-safe."""

from __future__ import annotations

import os
import random
import threading
import time
from datetime import datetime, timezone

from .acquisition import FetchFn, check_source
from .db import LearningDB
from .diffing import REVIEW_THRESHOLD
from .models import DocEvent, Source
from .registry import seed_sources
from .summarize import DEFAULT_LEARN_MODEL, llm_enabled, summarize_doc_event


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_due(source: Source, now: datetime) -> bool:
    if not source.last_checked:
        return True
    try:
        last = datetime.fromisoformat(source.last_checked)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    return (now - last).total_seconds() >= source.cadence_s


class LearningScheduler:
    """Checks due sources and (optionally) summarizes significant events within budget."""

    def __init__(
        self,
        db: LearningDB | None = None,
        interval_s: int | None = None,
        fetch_fn: FetchFn | None = None,
        mcp_probe_fn=None,
        jitter: float = 0.1,
    ):
        self.db = db or LearningDB()
        self.interval_s = interval_s if interval_s is not None else int(os.environ.get("SPRITEWARDEN_LEARN_INTERVAL", "3600"))
        self.fetch_fn = fetch_fn
        self.mcp_probe_fn = mcp_probe_fn
        self.jitter = jitter
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def due_sources(self) -> list[Source]:
        now = datetime.now(timezone.utc)
        return [s for s in self.db.list_sources(enabled_only=True) if _is_due(s, now)]

    def run_once(self) -> list[DocEvent]:
        """Check all due sources; seed the built-in set if none are registered."""
        sources = self.db.list_sources(enabled_only=True)
        if not sources:
            seed_sources(self.db)
        events: list[DocEvent] = []
        for source in self.due_sources():
            events.extend(check_source(source, self.db, fetch_fn=self.fetch_fn, mcp_probe_fn=self.mcp_probe_fn))

        if llm_enabled() and events:
            for event in events:
                if event.significance >= REVIEW_THRESHOLD and event.id is not None:
                    source = self.db.get_source(event.source_id)
                    if not source:
                        continue
                    what, how, why, _cost = summarize_doc_event(event, source, self.db)
                    self.db.update_event_summary(event.id, what, how, why, DEFAULT_LEARN_MODEL)
                    event.summary_what, event.summary_how, event.summary_why = what, how, why
                    event.model = DEFAULT_LEARN_MODEL

        if events:
            from .curation import proposals_from_events

            proposals_from_events(self.db)
        return events

    def start(self, daemon: bool = True) -> "LearningScheduler":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="learning-scheduler", daemon=daemon)
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                pass
            delay = self.interval_s * (1.0 + random.uniform(-self.jitter, self.jitter))
            self._stop.wait(max(1.0, delay))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
