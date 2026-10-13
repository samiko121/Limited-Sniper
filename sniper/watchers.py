"""Watchers poll Roblox for the watchlist through the lane pool.

  marketplace  apis.roblox.com/marketplace-items  120 items/request, ~55 ms, includes
               listing ids -> the main watcher (~700 items/s per lane)
  catalog      catalog.roblox.com items/details     120 items/request but heavily rate
               limited (~1 req/s); also learns collectible ids for new items
  economy      economy.roblox.com assets/{id}       1 item/request, ~500 ms, generous limit

Running several watchers over different endpoints spreads load across Roblox's
separate rate limits, like the old sniper did, but with shared rate control.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import httpx

from . import roblox
from .lanes import Lane
from .parsers import (
    meta_from_catalog,
    meta_from_economy,
    meta_from_marketplace,
    offers_from_catalog,
    offers_from_economy,
    offers_from_marketplace,
)

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)


class Watcher:
    name = "watcher"

    def __init__(self, engine: "Engine", worker: int):
        self.engine = engine
        self.worker = worker
        self.cursor = self.name  # workers of one watcher share a cursor

    async def tick(self, lane: Lane) -> int:  # returns the number of items checked
        raise NotImplementedError

    def has_work(self) -> bool:
        return bool(self.engine.watchlist.active_ids())

    async def run(self) -> None:
        e = self.engine
        while True:
            if e.paused or not self.has_work():
                await asyncio.sleep(0.5)
                continue
            lane = await e.lanes.acquire(self.name)
            t0 = time.perf_counter()
            try:
                n = await self.tick(lane)
                latency = time.perf_counter() - t0
                lane.ok(self.name, latency)
                if n:
                    e.stats.add_checks(n, latency)
                else:
                    await asyncio.sleep(0.25)
            except roblox.RateLimited:
                lane.rate_limited(self.name)
                e.stats.rate_limited += 1
            except roblox.Unauthorized:
                lane.failed()
                e.stats.errors += 1
                log.warning("%s: lane %s got 401 - check cookie expired?", self.name, lane.label)
            except roblox.RobloxHTTPError as err:
                lane.failed()
                e.stats.errors += 1
                log.debug("%s via %s: %s", self.name, lane.label, err)
            except httpx.HTTPError as err:
                lane.failed()
                e.stats.errors += 1
                log.debug("%s via %s: %s: %s", self.name, lane.label, type(err).__name__, err)
            except asyncio.CancelledError:
                raise
            except Exception:
                e.stats.errors += 1
                log.exception("%s crashed on one tick", self.name)
                await asyncio.sleep(1)


class MarketplaceWatcher(Watcher):
    name = "marketplace"

    def has_work(self) -> bool:
        wl = self.engine.watchlist
        return any(wl.collectible_id(i) for i in wl.active_ids())

    async def tick(self, lane: Lane) -> int:
        e = self.engine
        wl = e.watchlist
        ids = wl.next_batch(self.cursor, e.settings.marketplace_batch_size, lambda i: wl.collectible_id(i) is not None)
        if not ids:
            return 0
        cids = [c for c in (wl.collectible_id(i) for i in ids) if c]
        rows = await roblox.marketplace_details(lane.session, cids)
        if cids and not rows and not self.engine.marketplace_empty_warned:
            self.engine.marketplace_empty_warned = True
            e.stats.event(f"Marketplace returned 0 rows for {len(cids)} ids - Roblox may have changed the batch limit", logging.WARNING)
        for d in rows:
            asset_id = d.get("itemTargetId")
            if asset_id is None:
                continue
            wl.learn(int(asset_id), meta_from_marketplace(d))
            for offer in offers_from_marketplace(d):
                e.consider(offer)
        return len(rows)  # count what Roblox actually answered, not what we asked for


class CatalogWatcher(Watcher):
    name = "catalog"

    async def tick(self, lane: Lane) -> int:
        e = self.engine
        wl = e.watchlist
        ids = wl.next_batch(self.cursor, e.settings.catalog_batch_size, lambda i: not wl.meta_for(i).not_collectible)
        if not ids:
            return 0
        rows = await roblox.catalog_details(lane.session, ids)
        for d in rows:
            asset_id = d.get("id")
            if asset_id is None:
                continue
            asset_id = int(asset_id)
            wl.learn(asset_id, meta_from_catalog(d), e.stats)
            product_id = wl.meta_for(asset_id).collectible_product_id
            for offer in offers_from_catalog(d, product_id):
                e.consider(offer)
        return len(rows)


class EconomyWatcher(Watcher):
    name = "economy"

    async def tick(self, lane: Lane) -> int:
        e = self.engine
        wl = e.watchlist
        ids = wl.next_batch(self.cursor, 1, lambda i: not wl.meta_for(i).not_collectible)
        if not ids:
            return 0
        d = await roblox.economy_details(lane.session, ids[0])
        wl.learn(ids[0], meta_from_economy(d), e.stats)
        for offer in offers_from_economy(d):
            e.consider(offer)
        return 1


async def resolve_loop(engine: "Engine") -> None:
    """Learn the collectible ids of newly added items as fast as possible, so
    the marketplace watcher can include them."""
    wl = engine.watchlist
    while True:
        missing = wl.unresolved()
        if not missing:
            await asyncio.sleep(1)
            continue
        lane = await engine.lanes.acquire("catalog")
        try:
            rows = await roblox.catalog_details(lane.session, missing[:120])
            seen = set()
            for d in rows:
                if d.get("id") is not None:
                    seen.add(int(d["id"]))
                    wl.learn(int(d["id"]), meta_from_catalog(d), engine.stats)
            lane.ok("catalog", 0)
            # The catalog silently leaves some ids out. Double-check those one by
            # one on the economy endpoint before deciding they can't be bought.
            for asset_id in missing[:120]:
                if asset_id not in seen:
                    await _verify_single(engine, asset_id)
        except roblox.RateLimited:
            lane.rate_limited("catalog")
            engine.stats.rate_limited += 1
        except (roblox.RobloxHTTPError, httpx.HTTPError) as err:
            lane.failed()
            log.debug("resolver: %s", err)
            await asyncio.sleep(2)


async def _verify_single(engine: "Engine", asset_id: int) -> None:
    wl = engine.watchlist
    for _ in range(3):
        lane = await engine.lanes.acquire("economy")
        try:
            d = await roblox.economy_details(lane.session, asset_id)
        except roblox.RateLimited:
            lane.rate_limited("economy")
            continue
        except roblox.RobloxHTTPError as err:
            if err.status in (400, 404):
                d = {}
            else:
                lane.failed()
                continue
        except httpx.HTTPError:
            lane.failed()
            continue
        lane.ok("economy", 0)
        meta = meta_from_economy(d) if d else None
        if meta and meta.collectible_item_id:
            wl.learn(asset_id, meta, engine.stats)
        else:
            m = wl.meta_for(asset_id)
            m.not_collectible = True
            if d.get("Name"):
                m.name = d["Name"]
            engine.stats.event(
                f"Item {asset_id} ({m.name or '?'}) can't be bought on Roblox (removed from the catalog / not a collectible) - skipped",
                logging.WARNING,
            )
        return
