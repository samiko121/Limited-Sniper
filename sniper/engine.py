"""Wires everything together and owns the background tasks."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from . import __version__, roblox
from .buyer import Buyer
from .config import ConfigError, ConfigStore, Settings
from .history import History
from .lanes import LanePool
from .models import Offer
from .notify import Notifier
from .state import Stats, Watchlist
from .watchers import CatalogWatcher, EconomyWatcher, MarketplaceWatcher, resolve_loop

log = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        store: ConfigStore,
        buy_session: roblox.RobloxSession,
        lanes: LanePool,
        history: History,
        notifier: Notifier,
    ):
        self.store = store
        self.buy_session = buy_session
        self.lanes = lanes
        self.history = history
        self.notifier = notifier
        self.stats = Stats()
        self.watchlist = Watchlist()
        self.watchlist.set_items(self.settings.items)
        self.buyer = Buyer(buy_session, lambda: self.settings, self.watchlist, self.stats, history, notifier)
        self.paused = False
        self.stop_event = asyncio.Event()
        self.restart_requested = False
        self._tasks: list[asyncio.Task] = []

    @property
    def settings(self) -> Settings:
        assert self.store.settings is not None
        return self.store.settings

    # --- lifecycle -------------------------------------------------------------

    async def login(self) -> None:
        user_id, name = await roblox.get_authenticated_user(self.buy_session)
        self.buyer.user_id = user_id
        self.stats.user_id, self.stats.username = user_id, name
        self.stats.robux = await roblox.get_robux(self.buy_session, user_id)
        await self.buy_session.refresh_csrf()
        self.stats.event(f"Logged in as {name} ({user_id}) with {self.stats.robux:,} R$")

    def start(self) -> None:
        s = self.settings
        spawn = self._spawn
        spawn(resolve_loop(self), "resolver")
        for enabled, cls in (
            (s.watch_marketplace, MarketplaceWatcher),
            (s.watch_catalog, CatalogWatcher),
            (s.watch_economy, EconomyWatcher),
        ):
            if enabled:
                for i in range(s.workers_per_watcher):
                    spawn(cls(self, i).run(), f"{cls.name}-{i}")
        spawn(self._keepalive(), "keepalive")
        spawn(self._robux_refresher(), "robux")
        spawn(self._config_watcher(), "config")
        spawn(self.notifier.run(), "webhook")
        if s.auto_restart_minutes:
            spawn(self._auto_restart(s.auto_restart_minutes), "auto-restart")
        self.stats.task = "Checking"
        watchers = [n for n, on in (("marketplace", s.watch_marketplace), ("catalog", s.watch_catalog), ("economy", s.watch_economy)) if on]
        self.stats.event(
            f"Limited Sniper v{__version__} running: {len(self.watchlist.items)} items, "
            f"{len(self.lanes.lanes)} lane(s), watchers: {', '.join(watchers) or 'none'}"
            + (" [DRY RUN]" if s.dry_run else "")
        )

    def _spawn(self, coro, name: str) -> None:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        task.add_done_callback(self._task_done)
        self._tasks.append(task)

    def _task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.error("Background task %s stopped: %r", task.get_name(), exc, exc_info=exc)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.buyer.drain()

    def request_stop(self, restart: bool = False) -> None:
        self.restart_requested = restart
        self.stop_event.set()

    # --- called by watchers ------------------------------------------------------

    def consider(self, offer: Offer) -> None:
        if self.paused:
            return
        item = self.watchlist.get(offer.asset_id)
        if item is None or offer.price > item.max_price:
            return
        self.buyer.consider(offer, item)

    # --- control -------------------------------------------------------------------

    def apply_config(self) -> None:
        self.watchlist.set_items(self.settings.items)

    async def _config_watcher(self) -> None:
        """Pick up hand edits of config.json without a restart."""
        while True:
            await asyncio.sleep(2)
            if not self.store.changed_on_disk():
                continue
            try:
                self.store.load()
            except ConfigError as e:
                self.stats.event(f"config.json not reloaded: {e}", logging.ERROR)
                self.store.mark_seen()  # don't repeat the error every 2 seconds
                continue
            self.apply_config()
            self.stats.event(f"Reloaded config.json ({len(self.watchlist.items)} items)")

    async def _keepalive(self) -> None:
        """Keep the purchase connection warm so a buy doesn't pay for a TLS handshake."""
        last_csrf = time.monotonic()
        while True:
            await asyncio.sleep(20)
            try:
                await self.buy_session.client.get(f"{roblox.APIS}/marketplace-sales/v1/item/00000000-0000-0000-0000-000000000000/resellers?limit=1")
                if time.monotonic() - last_csrf > 600:
                    await self.buy_session.refresh_csrf()
                    last_csrf = time.monotonic()
            except httpx.HTTPError:
                pass

    async def _robux_refresher(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                self.stats.robux = await roblox.get_robux(self.buy_session, self.buyer.user_id)
            except roblox.Unauthorized:
                self.stats.event("Buy cookie is no longer valid (401). Update Buy_cookie.", logging.ERROR)
            except (roblox.RobloxHTTPError, httpx.HTTPError):
                pass

    async def _auto_restart(self, minutes: int) -> None:
        await asyncio.sleep(minutes * 60)
        self.stats.event("Auto restart")
        self.request_stop(restart=True)
