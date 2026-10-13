"""Commands shared by the console and the Discord bot."""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass

from . import __version__
from .config import AUTO, CLASSIC, UGC, WatchItem, parse_item_ref
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
    Help("list", "", "List manually added items (UGC and classic)"),
    Help("add", "<id|link> <max price> [buy limit]", "Watch a UGC limited"),
    Help("add_classic", "<id|link> <max price> [buy limit]", "Watch a classic limited at a fixed max price"),
    Help("mass_add", "<max price> <id> <id> ...", "Watch many UGC items with one price"),
    Help("deals", "", "Classic auto-deal settings and top watched deals"),
    Help("deal_percent", "<percent>", "Minimum % below value for classic auto deals"),
    Help("auto_deals", "<on|off>", "Turn classic auto deals from Rolimons on/off"),
    Help("remove", "<id|link>", "Stop watching an item"),
    Help("remove_all", "", "Clear the watchlist"),
    Help("price", "<id|link> <max price>", "Change an item's max price"),
    Help("price_all", "<max price>", "Change every manual item's max price"),
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

    def add(self, ref: str | int, max_price: str | int, buy_limit: str | int | None = None, category: str = UGC) -> str:
        asset_id, price = _item(ref), _price(max_price)
        limit = None
        if buy_limit not in (None, "", "none"):
            limit = _price(buy_limit)
            if limit < 1:
                raise CommandError("buy limit must be at least 1")
        if category not in (UGC, CLASSIC):
            raise CommandError("category must be ugc or classic")
        items = self._items()
        existed = asset_id in items
        items[asset_id] = WatchItem(asset_id, price, limit, category)
        self._save(items)
        verb = "Updated" if existed else "Added"
        label = "UGC" if category == UGC else "classic"
        return f"{verb} {label} item {asset_id} with max price {price} R$" + (f" and buy limit {limit}" if limit else "")

    def mass_add(self, max_price: str | int, refs: list[str], category: str = UGC) -> str:
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
            items[asset_id] = WatchItem(asset_id, price, items[asset_id].buy_limit if asset_id in items else None, category)
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
            if asset_id in self.e.auto_items:
                raise CommandError(f"{asset_id} is a classic auto deal - add it to Classic.Auto_deals.Exclude_ids to skip it")
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
        items[asset_id] = WatchItem(asset_id, price, items[asset_id].buy_limit, items[asset_id].category)
        self._save(items)
        return f"Max price of {asset_id} is now {price} R$"

    def price_all(self, max_price: str | int) -> str:
        price = _price(max_price)
        items = {k: WatchItem(k, price, v.buy_limit, v.category) for k, v in self._items().items()}
        self._save(items)
        return f"Max price of all {len(items)} item(s) is now {price} R$"

    def limit(self, ref: str | int, value: str | int | None) -> str:
        asset_id = _item(ref)
        items = self._items()
        if asset_id not in items:
            raise CommandError(f"{asset_id} is not in the watchlist")
        limit = None if value in (None, "", "none", "0", 0) else _price(value)
        items[asset_id] = WatchItem(asset_id, items[asset_id].max_price, limit, items[asset_id].category)
        self._save(items)
        return f"Buy limit of {asset_id} " + (f"set to {limit}" if limit else "cleared")

    def list_items(self) -> list[tuple[int, str, int, int | None, str]]:
        """Manual items, UGC first then classic."""
        wl = self.e.watchlist
        order = {UGC: 0, CLASSIC: 1}
        return [
            (i, (wl.meta.get(i).name if wl.meta.get(i) else None) or "?", it.max_price, it.buy_limit, it.category)
            for i, it in sorted(self.e.settings.items.items(), key=lambda kv: (order.get(kv[1].category, 2), kv[0]))
        ]

    # --- classic auto deals ----------------------------------------------------------

    def deals(self, top: int = 10) -> list[str]:
        s, e = self.e.settings, self.e
        roli = e.rolimons
        age = f"{(time.time() - roli.updated_at) / 60:.0f} min ago" if roli and roli.updated_at else "never"
        lines = [
            f"Classic auto deals: {'ON' if s.auto_deals_enabled else 'OFF'} - {len(e.auto_items)} items watched",
            f"  Only valued items: {s.only_valued_items}   Skip known projecteds: {s.skip_known_projecteds}",
            f"  No deals below: {s.min_deal_percent:g}%   Deal calculation: {s.deal_calculation.replace('_or_', '/')}",
            f"  Ignore prices below {s.ignore_prices_below:,} and above {s.ignore_prices_above:,} R$",
            f"  Rolimons values updated: {age}",
        ]
        if e.auto_items:
            lines.append(f"  Highest value items (buy at or below):")
            best = sorted(e.auto_items.values(), key=lambda i: -(i.ref_value or 0))[:top]
            for it in best:
                name = e.watchlist.meta_for(it.asset_id).name or str(it.asset_id)
                lines.append(f"    {name[:32]:<32} value {it.ref_value:>10,}  buy <= {it.max_price:>10,}")
        return lines

    def deal_percent(self, value: str | float) -> str:
        try:
            pct = float(value)
        except (TypeError, ValueError):
            raise CommandError(f"'{value}' is not a number") from None
        self.e.store.update_setting("Classic.Auto_deals", "Min_deal_percent", pct)
        self.e.apply_config()
        return f"Classic auto deals now need >= {pct:g}% below value ({len(self.e.auto_items)} items)"

    def auto_deals(self, value: str | bool) -> str:
        on = value if isinstance(value, bool) else str(value).lower() in ("on", "true", "1", "yes")
        self.e.store.update_setting("Classic.Auto_deals", "Enabled", on)
        self.e.apply_config()
        if on and self.e.rolimons and not self.e.rolimons.items:
            return "Classic auto deals ON - values load on the next Rolimons refresh (or restart)"
        return f"Classic auto deals {'ON' if on else 'OFF'} ({len(self.e.auto_items)} items)"

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
            "Items": "UGC {} | classic {} | classic auto {}".format(*self.e.counts().values()),
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
            "Lanes": f"{sum(1 for l in self.e.lanes.lanes if l.status == 'ok')}/{len(self.e.lanes.lanes)} at full speed",
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
            f"{l.label:<28} {l.status:<12} {l.describe_rates()}  {l.requests:>7} req  {l.rate_limits:>4} x429"
            for l in self.e.lanes.lanes
        ]
