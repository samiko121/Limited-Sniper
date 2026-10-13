"""Classic limited values from Rolimons -> automatic deal watchlist.

Rolimons publishes the value / RAP / projected flag of every classic limited at
https://www.rolimons.com/itemapi/itemdetails (one public GET, nothing is sent
about you). The sniper keeps that data in memory (and in data/rolimons.json so
a restart works even if Rolimons is down) and turns it into a max price per
item:

    reference = value            (Deal_calculation "value")
              = RAP              (Deal_calculation "rap")
              = value, else RAP  (Deal_calculation "value_or_rap", default)
    deal %    = (reference - price) / reference * 100
    max price = largest price with deal % >= Min_deal_percent

so when a listing comes in the decision is a single integer comparison.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import AUTO, Settings, WatchItem

log = logging.getLogger(__name__)

ITEMDETAILS_URL = "https://www.rolimons.com/itemapi/itemdetails"


@dataclass(frozen=True)
class RoliItem:
    asset_id: int
    name: str
    acronym: str
    rap: int | None
    value: int | None  # None = not valued by Rolimons
    projected: bool
    demand: int | None


def _num(v: object) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        return None
    return int(v)


def parse_itemdetails(data: dict) -> dict[int, RoliItem]:
    """itemdetails rows are [name, acronym, rap, value, default_value, demand,
    trend, projected, hyped, rare]; -1 means "none"."""
    if not data.get("success") or not isinstance(data.get("items"), dict):
        raise ValueError("unexpected Rolimons response")
    out: dict[int, RoliItem] = {}
    for key, row in data["items"].items():
        if not isinstance(row, list) or len(row) < 8:
            continue
        try:
            asset_id = int(key)
        except ValueError:
            continue
        out[asset_id] = RoliItem(
            asset_id=asset_id,
            name=str(row[0]),
            acronym=str(row[1] or ""),
            rap=_num(row[2]),
            value=_num(row[3]) or None,
            projected=row[7] == 1,
            demand=_num(row[5]),
        )
    return out


def reference_price(item: RoliItem, calculation: str) -> int | None:
    if calculation == "value":
        return item.value
    if calculation == "rap":
        return item.rap or None
    return item.value or item.rap or None


def max_price_for(reference: int, min_deal_percent: float) -> int:
    """Largest whole price whose discount vs reference is >= min_deal_percent."""
    return math.floor(reference * (100 - min_deal_percent) / 100 + 1e-9)


def deal_percent(reference: int, price: int) -> float:
    return (reference - price) / reference * 100


def build_auto_items(items: dict[int, RoliItem], s: Settings) -> dict[int, WatchItem]:
    out: dict[int, WatchItem] = {}
    for asset_id, it in items.items():
        if asset_id in s.auto_exclude_ids:
            continue
        if s.only_valued_items and not it.value:
            continue
        if s.skip_known_projecteds and it.projected:
            continue
        ref = reference_price(it, s.deal_calculation)
        if not ref:
            continue
        max_price = min(max_price_for(ref, s.min_deal_percent), s.ignore_prices_above)
        if max_price < s.ignore_prices_below:
            continue  # no price inside the allowed range could ever be a deal
        out[asset_id] = WatchItem(
            asset_id, max_price, None, AUTO, min_price=s.ignore_prices_below, ref_value=ref,
        )
    return out


class RolimonsValues:
    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self.items: dict[int, RoliItem] = {}
        self.updated_at = 0.0
        self.client = httpx.AsyncClient(timeout=20, trust_env=False, headers={"Accept": "application/json"})

    def load_cache(self) -> bool:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.items = parse_itemdetails(data["itemdetails"])
            self.updated_at = float(data["fetched_at"])
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    async def refresh(self) -> bool:
        """Returns True if the data changed."""
        r = await self.client.get(ITEMDETAILS_URL)
        r.raise_for_status()
        data = r.json()
        items = parse_itemdetails(data)
        changed = items != self.items
        self.items, self.updated_at = items, time.time()
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fetched_at": self.updated_at, "itemdetails": data}), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError as e:
            log.debug("could not write Rolimons cache: %s", e)
        return changed

    def name(self, asset_id: int) -> str | None:
        it = self.items.get(asset_id)
        return it.name if it else None

    async def aclose(self) -> None:
        await self.client.aclose()
