"""Commands shared by the console and the Discord bot."""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass

from . import __version__
from .config import WatchItem, parse_item_ref
from .engine import Engine


class CommandError(Exception):
    pass


@dataclass
class Help:
    name: str
    args: str
    text: str


HELP = [
    Help("stats", "", "Show sniper statistics"),
    Help("list", "", "List watched items and their max prices"),
    Help("add", "<id|link> <max price> [buy limit]", "Watch an item"),
    Help("mass_add", "<max price> <id> <id> ...", "Watch many items with one price"),
    Help("remove", "<id|link>", "Stop watching an item"),
    Help("remove_all", "", "Clear the watchlist"),
    Help("price", "<id|link> <max price>", "Change an item's max price"),
    Help("price_all", "<max price>", "Change every item's max price"),
    Help("limit", "<id|link> <n|none>", "Set/clear an item's buy limit"),
    Help("focus", "<id|link> <max price>", "Only check this one item"),
    Help("unfocus", "", "Go back to the whole watchlist"),
    Help("pause", "", "Stop checking and buying"),
    Help("resume", "", "Continue after pause"),
    Help("dry_run", "<on|off>", "Simulate purchases without buying"),
    Help("history", "[n]", "Last purchases / attempts"),
    Help("lanes", "", "Show proxy/alt lanes"),
    Help("restart", "", "Restart the sniper"),
]


def _price(value: str | int) -> int:
    try:
        p = int(value)
    except (TypeError, ValueError):
        raise CommandError(f"'{value}' is not a whole number") from None
    if p < 0:
        raise CommandError("price can not be negative")
    return p


def _item(ref: str | int) -> int:
    try:
        return parse_item_ref(ref)
    except ValueError as e:
        raise CommandError(str(e)) from None


class Commands:
    def __init__(self, engine: Engine):
        self.e = engine

    def _items(self) -> dict[int, WatchItem]:
        return dict(self.e.settings.items)

    def _save(self, items: dict[int, WatchItem]) -> None:
        self.e.store.set_items(items)
        self.e.apply_config()

    # --- watchlist ---------------------------------------------------------------

    def add(self, ref: str | int, max_price: str | int, buy_limit: str | int | None = None) -> str:
        asset_id, price = _item(ref), _price(max_price)
        limit = None
        if buy_limit not in (None, "", "none"):
            limit = _price(buy_limit)
            if limit < 1:
                raise CommandError("buy limit must be at least 1")
        items = self._items()
        existed = asset_id in items
        items[asset_id] = WatchItem(asset_id, price, limit)
        self._save(items)
        verb = "Updated" if existed else "Added"
        return f"{verb} {asset_id} with max price {price} R$" + (f" and buy limit {limit}" if limit else "")

    def mass_add(self, max_price: str | int, refs: list[str]) -> str:
        price = _price(max_price)
        items = self._items()
        added, bad = 0, []
        for ref in refs:
            try:
                asset_id = parse_item_ref(ref)
            except ValueError:
                bad.append(ref)
                continue
            if asset_id not in items:
                added += 1
            items[asset_id] = WatchItem(asset_id, price, items[asset_id].buy_limit if asset_id in items else None)
        if added or len(refs) > len(bad):
            self._save(items)
        msg = f"Added {added} item(s) at {price} R$"
        if bad:
            msg += f"; ignored invalid: {', '.join(bad[:10])}" + (" ..." if len(bad) > 10 else "")
        return msg

    def remove(self, ref: str | int) -> str:
        asset_id = _item(ref)
        items = self._items()
        if asset_id not in items:
            raise CommandError(f"{asset_id} is not in the watchlist")
        del items[asset_id]
        self._save(items)
        return f"Removed {asset_id}"

    def remove_all(self) -> str:
        n = len(self.e.settings.items)
        self._save({})
        return f"Removed all {n} item(s)"

    def price(self, ref: str | int, max_price: str | int) -> str:
        asset_id, price = _item(ref), _price(max_price)
        items = self._items()
        if asset_id not in items:
            raise CommandError(f"{asset_id} is not in the watchlist")
        items[asset_id] = WatchItem(asset_id, price, items[asset_id].buy_limit)
        self._save(items)
        return f"Max price of {asset_id} is now {price} R$"

    def price_all(self, max_price: str | int) -> str:
        price = _price(max_price)
        items = {k: WatchItem(k, price, v.buy_limit) for k, v in self._items().items()}
        self._save(items)
        return f"Max price of all {len(items)} item(s) is now {price} R$"

    def limit(self, ref: str | int, value: str | int | None) -> str:
        asset_id = _item(ref)
        items = self._items()
        if asset_id not in items:
            raise CommandError(f"{asset_id} is not in the watchlist")
        limit = None if value in (None, "", "none", "0", 0) else _price(value)
        items[asset_id] = WatchItem(asset_id, items[asset_id].max_price, limit)
        self._save(items)
        return f"Buy limit of {asset_id} " + (f"set to {limit}" if limit else "cleared")

    def list_items(self) -> list[tuple[int, str, int, int | None]]:
        wl = self.e.watchlist
        return [
            (i, (wl.meta.get(i).name if wl.meta.get(i) else None) or "?", it.max_price, it.buy_limit)
            for i, it in sorted(self.e.settings.items.items())
        ]

    # --- run control ---------------------------------------------------------------

    def focus(self, ref: str | int, max_price: str | int) -> str:
        asset_id, price = _item(ref), _price(max_price)
        base = self.e.settings.items.get(asset_id)
        self.e.watchlist.focus = WatchItem(asset_id, price, base.buy_limit if base else None)
        return f"Focusing only on {asset_id} at max {price} R$ (use unfocus to go back)"

    def unfocus(self) -> str:
        if self.e.watchlist.focus is None:
            raise CommandError("nothing is focused")
        asset_id = self.e.watchlist.focus.asset_id
        self.e.watchlist.focus = None
        return f"Stopped focusing {asset_id}"

    def pause(self) -> str:
        self.e.paused = True
        self.e.stats.task = "Paused"
        return "Paused"

    def resume(self) -> str:
        self.e.paused = False
        self.e.stats.task = "Checking"
        return "Resumed"

    def dry_run(self, value: str | bool) -> str:
        on = value if isinstance(value, bool) else str(value).lower() in ("on", "true", "1", "yes")
        self.e.store.update_setting("Safety", "Dry_run", on)
        return f"Dry run {'ON - nothing will be bought' if on else 'OFF - purchases are live'}"

    def restart(self) -> str:
        self.e.request_stop(restart=True)
        return "Restarting..."

    # --- info ----------------------------------------------------------------------------

    def stats(self) -> dict[str, str]:
        st, s = self.e.stats, self.e.settings
        mode = []
        if self.e.paused:
            mode.append("PAUSED")
        if s.dry_run:
            mode.append("DRY RUN")
        if self.e.watchlist.focus:
            mode.append(f"FOCUS {self.e.watchlist.focus.asset_id}")
        return {
            "Account": f"{st.username} ({st.user_id})",
            "Robux": f"{st.robux:,}" if st.robux is not None else "?",
            "Items": str(len(s.items)),
            "Watching": self.e.watchlist.describe(),
            "Uptime": str(datetime.timedelta(seconds=int(st.uptime))),
            "Task": st.task,
            "Mode": ", ".join(mode) or "live",
            "Checks": f"{st.checks:,} ({st.checks_per_second:.1f}/s)",
            "Latency": f"{st.latency * 1000:.0f} ms",
            "Bought": f"{st.buys} ({st.spent:,} R$)",
            "Attempts / fails": f"{st.attempts} / {st.fails}",
            "Spent today": f"{self.e.history.spent_today():,} R$",
            "Errors / 429s": f"{st.errors} / {st.rate_limited}",
            "Last bought": st.last_bought or "-",
            "Lanes": f"{sum(1 for l in self.e.lanes.lanes if l.status == 'ok')}/{len(self.e.lanes.lanes)} ok",
            "Version": __version__,
        }

    def history(self, n: str | int = 10) -> list[str]:
        try:
            limit = max(1, min(int(n), 50))
        except ValueError:
            raise CommandError("n must be a number") from None
        out = []
        for p in self.e.history.recent(limit):
            when = time.strftime("%m-%d %H:%M", time.localtime(p.ts))
            serial = f" #{p.serial}" if p.serial else ""
            err = f" ({p.error})" if p.error else ""
            out.append(f"{when} {p.status:<7} {p.price:>6} R$ {p.name}{serial}{err}")
        return out or ["No purchases yet"]

    def lanes(self) -> list[str]:
        return [
            f"{l.label:<28} {l.status:<8} {l.rps:5.1f} rps  {l.requests:>7} req  {l.rate_limits:>4} x429"
            for l in self.e.lanes.lanes
        ]
