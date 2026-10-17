"""Decides whether an offer may be bought and buys it.

Safety rules are checked right before every purchase:
  * price <= the item's max price (re-checked after fetching the live listing)
  * Safety.Max_price_per_purchase, Safety.Daily_spend_limit, Safety.Keep_robux_reserve
  * per-item / global buy limit and Roblox's own per-user quantity limit
  * the same listing is never attempted twice at the same time
Purchases use one idempotency key per attempt, so a network retry can never
buy the same thing twice.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Callable

import httpx

from . import roblox
from .config import Settings, WatchItem
from .history import History, Purchase
from .models import RESALE, STOCK, Offer
from .notify import Notifier
from .parsers import offer_from_reseller, offers_from_economy
from .state import Stats, Watchlist

log = logging.getLogger(__name__)

OWNED_TTL = 300.0


def effective_limit(item: WatchItem, settings: Settings, roblox_limit: int | None) -> int | None:
    limits = [l for l in (item.buy_limit if item.buy_limit is not None else settings.global_buy_limit, roblox_limit) if l]
    return min(limits) if limits else None


def deal_text(offer: Offer, item: WatchItem) -> str:
    if not item.ref_value:
        return ""
    pct = (item.ref_value - offer.price) / item.ref_value * 100
    return f" - {pct:.0f}% below value {item.ref_value:,}"


def guard_reason(
    offer: Offer,
    item: WatchItem,
    settings: Settings,
    *,
    robux: int | None,
    spent_today: int,
    pending: int,
    owned: int | None,
) -> str | None:
    """Return why the offer must NOT be bought, or None if it is fine."""
    if offer.price > item.max_price:
        return f"price {offer.price} > max {item.max_price}"
    if offer.price < item.min_price:
        return f"price {offer.price} < Ignore_prices_below {item.min_price}"
    if settings.max_price_per_purchase and offer.price > settings.max_price_per_purchase:
        return f"price {offer.price} > Max_price_per_purchase {settings.max_price_per_purchase}"
    if settings.daily_spend_limit and spent_today + pending + offer.price > settings.daily_spend_limit:
        return f"daily spend limit {settings.daily_spend_limit} R$ reached"
    if robux is not None and offer.price > 0 and robux - pending - offer.price < settings.robux_reserve:
        return f"not enough Robux ({robux}, reserve {settings.robux_reserve})"
    limit = effective_limit(item, settings, offer.quantity_limit_per_user)
    if limit is not None and owned is not None and owned >= limit:
        return f"buy limit reached ({owned}/{limit})"
    return None


class Buyer:
    def __init__(
        self,
        session: roblox.RobloxSession,
        settings: Callable[[], Settings],
        watchlist: Watchlist,
        stats: Stats,
        history: History,
        notifier: Notifier,
        max_concurrent: int = 4,
    ):
        self.session = session
        self.settings = settings
        self.watchlist = watchlist
        self.stats = stats
        self.history = history
        self.notifier = notifier
        self.user_id = 0
        self.inflight: set[tuple] = set()
        self.cooldown: dict[tuple, float] = {}
        self.owned: dict[str, tuple[int, float]] = {}
        self.pending = 0
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max_concurrent)
        self._item_locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()

    # -- entry point from watchers (must stay cheap) ---------------------------

    def consider(self, offer: Offer, item: WatchItem) -> None:
        if offer.price > item.max_price or offer.price < item.min_price:
            return
        key = offer.key
        now = time.monotonic()
        if key in self.inflight or self.cooldown.get(key, 0) > now:
            return
        if len(self.cooldown) > 5000:
            self.cooldown = {k: v for k, v in self.cooldown.items() if v > now}
        self.inflight.add(key)
        task = asyncio.get_running_loop().create_task(self._attempt(offer, item, key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _attempt(self, offer: Offer, item: WatchItem, key: tuple) -> None:
        cooldown = 3.0
        try:
            async with self._sem:
                completed = await self._complete(offer)
                if completed is None:
                    return
                # Different watchers can detect the same listing with different
                # partial data. One lock per item serializes them (and keeps buy
                # limits exact); the completed listing key dedupes them.
                lock = self._item_locks.setdefault(completed.collectible_item_id, asyncio.Lock())
                async with lock:
                    ckey = completed.key
                    if ckey != key and self.cooldown.get(ckey, 0) > time.monotonic():
                        return
                    cooldown = await self._guarded_buy(completed, item)
                    self.cooldown[ckey] = time.monotonic() + cooldown
        except Exception:
            self.stats.errors += 1
            log.exception("Unexpected error while buying %s", offer.name)
        finally:
            self.inflight.discard(key)
            self.cooldown[key] = time.monotonic() + cooldown

    async def _complete(self, offer: Offer) -> Offer | None:
        """Fill in listing details that the detecting endpoint did not return."""
        if offer.complete:
            return offer
        try:
            if offer.kind == RESALE:
                row = await roblox.lowest_reseller(self.session, offer.collectible_item_id)
                return offer_from_reseller(row, offer) if row else None
            data = await roblox.economy_details(self.session, offer.asset_id)
            for o in offers_from_economy(data):
                if o.kind == STOCK:
                    return o
            return None
        except (roblox.RobloxHTTPError, httpx.HTTPError) as e:
            log.debug("Could not complete offer for %s: %s", offer.name, e)
            if offer.kind == RESALE and offer.collectible_product_id and offer.instance_id:
                return offer  # buy without seller id rather than miss it
            return None

    async def _owned(self, cid: str) -> int | None:
        cached = self.owned.get(cid)
        if cached and time.monotonic() - cached[1] < OWNED_TTL:
            return cached[0]
        try:
            count = await roblox.owned_count(self.session, cid, self.user_id)
        except (roblox.RobloxHTTPError, httpx.HTTPError) as e:
            log.warning("Could not check owned copies (%s) - skipping to respect the buy limit", e)
            return None
        self.owned[cid] = (count, time.monotonic())
        return count

    async def _guarded_buy(self, offer: Offer, item: WatchItem) -> float:
        settings = self.settings()
        limit = effective_limit(item, settings, offer.quantity_limit_per_user)
        owned = None
        if limit is not None:
            owned = await self._owned(offer.collectible_item_id)
            if owned is None:
                return 10.0

        async with self._lock:
            reason = guard_reason(
                offer, item, settings,
                robux=self.stats.robux, spent_today=self.history.spent_today(),
                pending=self.pending, owned=owned,
            )
            if reason:
                log.info("Skipping %s (%s R$): %s", offer.name, offer.price, reason)
                return 30.0 if "limit" in reason or "Robux" in reason else 3.0
            self.pending += offer.price

        try:
            if settings.dry_run:
                self.stats.event(f"[DRY RUN] would buy {offer.name} ({offer.kind}) for {offer.price:,} R${deal_text(offer, item)}", logging.WARNING)
                self.history.add(Purchase(time.time(), offer.asset_id, offer.name, offer.price, offer.kind, "dry_run", offer.serial, None))
                self.notifier.dry_run(offer)
                # A listing that would have been bought stays listed in dry run;
                # report it once, like a real buy, instead of every minute.
                return 600.0 if offer.kind == RESALE else 60.0
            return await self._buy(offer, item)
        finally:
            async with self._lock:
                self.pending -= offer.price

    async def _buy(self, offer: Offer, item: WatchItem) -> float:
        self.stats.task = "Buying"
        self.stats.attempts += 1
        key = str(uuid.uuid4())
        t0 = time.perf_counter()
        result = None
        for attempt in (0, 1):
            try:
                result = await roblox.purchase(self.session, offer, self.user_id, key)
                break
            except httpx.TransportError as e:
                if attempt == 1:
                    self._failed(offer, f"network error: {e}")
                    return 3.0
        assert result is not None
        took = (time.perf_counter() - t0) * 1000
        self.stats.task = "Checking"

        if result.success:
            self.stats.buys += 1
            self.stats.spent += offer.price
            self.stats.last_bought = offer.name
            cached = self.owned.get(offer.collectible_item_id)
            if cached:
                self.owned[offer.collectible_item_id] = (cached[0] + 1, cached[1])
            if self.stats.robux is not None:
                self.stats.robux -= offer.price
            self.stats.event(f"BOUGHT {offer.name} ({offer.kind}) for {offer.price:,} R$ in {took:.0f} ms{deal_text(offer, item)}")
            asyncio.get_running_loop().create_task(self._after_buy(offer))
            # a resale listing is gone once bought; original stock can be bought
            # again (up to the buy limit) as long as it stays under max price
            return 600.0 if offer.kind == RESALE else 1.0

        self._failed(offer, result.error or f"HTTP {result.status}")
        if result.insufficient_funds:
            return 60.0
        if result.status == 401:
            self.stats.event("Buy cookie was rejected (401) - is it still valid?", logging.ERROR)
            return 60.0
        return 5.0 if result.rate_limited else 2.0

    def _failed(self, offer: Offer, reason: str) -> None:
        self.stats.fails += 1
        self.stats.event(f"Failed to buy {offer.name} for {offer.price} R$: {reason}", logging.WARNING)
        self.history.add(Purchase(time.time(), offer.asset_id, offer.name, offer.price, offer.kind, "failed", None, reason[:300]))
        self.notifier.failed(offer, reason)

    async def _after_buy(self, offer: Offer) -> None:
        if offer.serial is None:
            try:
                offer.serial = await asyncio.wait_for(
                    roblox.latest_serial(self.session, offer.collectible_item_id, self.user_id), 5
                )
            except Exception:
                pass
        self.history.add(Purchase(time.time(), offer.asset_id, offer.name, offer.price, offer.kind, "bought", offer.serial, None))
        try:
            self.stats.robux = await roblox.get_robux(self.session, self.user_id)
        except Exception:
            pass
        self.notifier.bought(offer, self.stats.robux)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
