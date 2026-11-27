# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""The background refresh loop and the caches every request reads from.

A module subclasses `State` and implements `build()`: read its source, return the full graph. The
loop keeps the last good graph when a refresh fails, so the page stays up while a source is down.
"""
from __future__ import annotations

import logging
import threading
import time

from .scale import Index

log = logging.getLogger(__name__)


class State:
    refresh_seconds = 30
    index_options: dict = {}           # hooks passed to scale.Index (workload_cols, call_cols, search_extra, text_fields)

    def __init__(self):
        self.started = time.time()
        self.graph: dict | None = None
        self.error: str | None = None
        self.updated_at = 0.0
        self.build_seconds = 0.0
        self._lock = threading.Lock()
        self._index: Index | None = None
        self._views: dict[tuple, tuple] = {}
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()

    # ------------------------------------------------------------------ for the module
    def build(self) -> tuple[dict, str | None]:
        """-> (graph, error). `error` says the source failed and the graph is built from older data."""
        raise NotImplementedError

    def on_start(self) -> None:
        pass

    def on_stop(self) -> None:
        pass

    # ------------------------------------------------------------------ loop
    def start(self) -> None:
        self.on_start()
        threading.Thread(target=self._loop, name="refresh", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.on_stop()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except Exception as err:  # noqa: BLE001 - keep serving the last good graph
                log.exception("refresh failed")
                self.error = f"{type(err).__name__}: {err}"
            self._stop.wait(self.refresh_seconds)

    def refresh_once(self) -> dict:
        with self._refresh_lock:
            started = time.monotonic()
            graph, error = self.build()
            self.build_seconds = round(time.monotonic() - started, 2)
            graph.setdefault("sources", {})["buildSeconds"] = self.build_seconds
            with self._lock:
                self.graph = graph
                self.updated_at = time.time()
                self.error = error
                self._index = None
                self._views.clear()
            return graph

    # ------------------------------------------------------------------ what the API reads
    def current(self) -> dict | None:
        with self._lock:
            return self.graph

    def index(self) -> Index | None:
        with self._lock:
            if self._index is None and self.graph is not None:
                self._index = Index(self.graph, **self.index_options)
            return self._index

    def view(self, key: tuple, build):
        """One screen's worth of the graph, cached until the next refresh."""
        with self._lock:
            hit = self._views.get(key)
        if hit is None:
            hit = build()
            with self._lock:
                if len(self._views) > 24:
                    self._views.pop(next(iter(self._views)))
                self._views[key] = hit
        return hit
