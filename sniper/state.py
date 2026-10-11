"""Shared runtime state: statistics, event log and the watchlist."""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import fields
from typing import Callable

from .config import WatchItem
from .models import ItemMeta

log = logging.getLogger("sniper")


class Stats:
    def __init__(self) -> None:
        self.started_at = time.time()
        self.username = "?"
        self.user_id = 0
        self.robux: int | None = None
        self.checks = 0
        self.requests = 0
        self.errors = 0
        self.rate_limited = 0
        self.attempts = 0
        self.buys = 0
        self.fails = 0
        self.spent = 0
        self.last_bought: str | None = None
        self.latency = 0.0
        self.task = "Starting"
        self.events: deque[tuple[float, str, str]] = deque(maxlen=200)
        self._check_window: deque[tuple[float, int]] = deque()

    def event(self, msg: str, level: int = logging.INFO) -> None:
        self.events.append((time.time(), logging.getLevelName(level), msg))
        log.log(level, msg)

    def add_checks(self, n: int, latency: float) -> None:
        now = time.monotonic()
        self.checks += n
        self.requests += 1
        self.latency = latency if not self.latency else self.latency * 0.9 + latency * 0.1
        self._check_window.append((now, n))
        while self._check_window and now - self._check_window[0][0] > 10:
            self._check_window.popleft()

    @property
    def checks_per_second(self) -> float:
        if not self._check_window:
            return 0.0
        return sum(n for _, n in self._check_window) / 10.0

    @property
    def uptime(self) -> float:
        return time.time() - self.started_at


class Watchlist:
    """The items being sniped plus everything learned about them."""

    def __init__(self) -> None:
        self.items: dict[int, WatchItem] = {}
        self.meta: dict[int, ItemMeta] = {}
        self.focus: WatchItem | None = None
        self._order: list[int] = []
        self._cursors: dict[str, int] = {}
        self._warned: set[int] = set()

    def set_items(self, items: dict[int, WatchItem]) -> None:
        self.items = dict(items)
        self._order = sorted(self.items)
        self._cursors = {k: (v % len(self._order) if self._order else 0) for k, v in self._cursors.items()}

    def get(self, asset_id: int) -> WatchItem | None:
        if self.focus is not None:
            return self.focus if self.focus.asset_id == asset_id else None
        return self.items.get(asset_id)

    def active_ids(self) -> list[int]:
        return [self.focus.asset_id] if self.focus else list(self._order)

    def next_batch(self, cursor: str, n: int, predicate: Callable[[int], bool] | None = None) -> list[int]:
        """Round-robin over the watchlist; each watcher keeps its own cursor."""
        order = self.active_ids()
        if not order:
            return []
        start = self._cursors.get(cursor, 0) % len(order)
        out: list[int] = []
        scanned = 0
        while len(out) < n and scanned < len(order):
            asset_id = order[(start + scanned) % len(order)]
            scanned += 1
            if predicate is None or predicate(asset_id):
                out.append(asset_id)
        self._cursors[cursor] = (start + scanned) % len(order)
        return out

    def meta_for(self, asset_id: int) -> ItemMeta:
        return self.meta.setdefault(asset_id, ItemMeta())

    def learn(self, asset_id: int, new: ItemMeta, stats: Stats | None = None) -> None:
        cur = self.meta_for(asset_id)
        for f in fields(ItemMeta):
            value = getattr(new, f.name)
            if f.name == "not_collectible":
                continue
            if value is not None and value != getattr(cur, f.name):
                setattr(cur, f.name, value)
        if cur.collectible_item_id:
            cur.not_collectible = False
        elif new.not_collectible:
            cur.not_collectible = True
            if stats and asset_id in self.items and asset_id not in self._warned:
                self._warned.add(asset_id)
                stats.event(f"Item {asset_id} ({cur.name or '?'}) is not a limited/collectible - it can not be sniped", logging.WARNING)

    def collectible_id(self, asset_id: int) -> str | None:
        m = self.meta.get(asset_id)
        return m.collectible_item_id if m else None

    def unresolved(self) -> list[int]:
        return [
            i for i in self.active_ids()
            if not (self.meta.get(i) and (self.meta[i].collectible_item_id or self.meta[i].not_collectible))
        ]

    def describe(self, limit: int = 3) -> str:
        ids = self.active_ids()
        head = ", ".join(str(i) for i in ids[:limit])
        more = f" (+{len(ids) - limit} more)" if len(ids) > limit else ""
        return (head + more) if ids else "nothing"
