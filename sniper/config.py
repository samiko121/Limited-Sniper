"""Loading, validating, migrating and atomically saving config.json."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "Cookies": {
        "Buy_cookie": "",
        "Check_cookies": [],
        "Use_buy_cookie_for_checks": False,
        "Refresh_on_start": False,
    },
    # UGC limiteds (new collectibles made by players): manual ids + max price
    "UGC": {"Items": {}},
    # Classic Roblox limiteds: manual ids and/or automatic deals from Rolimons values
    "Classic": {
        "Items": {},
        "Auto_deals": {
            "Enabled": True,
            "Only_valued_items": True,
            "Skip_known_projecteds": True,
            "Min_deal_percent": 26,
            "Ignore_prices_above": 1000000,
            "Ignore_prices_below": 1000,
            "Deal_calculation": "value_or_rap",
            "Refresh_minutes": 2,
            "Exclude_ids": [],
        },
    },
    "Global_buy_limit": {"Enabled": False, "Limit": 3},
    "Safety": {
        "Dry_run": False,
        "Max_price_per_purchase": 0,
        "Daily_spend_limit": 0,
        "Keep_robux_reserve": 0,
    },
    "Watchers": {
        "Marketplace_batch": True,
        "Catalog_batch": True,
        "Economy_single": True,
        "Marketplace_batch_size": 120,
        "Catalog_batch_size": 120,
        "Rate_per_lane": {"Marketplace": 2, "Catalog": 0.5, "Economy": 15},
        "Workers": {"Marketplace": 2, "Catalog": 1, "Economy": 8},
    },
    "Proxies": {
        "Enabled": False,
        "File": "proxies.txt",
        "Proxies_only": False,
        "Send_check_cookie": False,
        "Max_failures": 5,
        "Tor": {"Enabled": False, "Path": "", "Instances": 10, "Start_port": 9080},
    },
    "Webhook": {"Enabled": False, "Url": "", "Send_fails": False, "UserID_To_Ping": 0},
    "Discord": {
        "Enabled": False,
        "Token": "",
        "Owner_ids": [],
        "Startup_message": False,
        "Prefix": {"Enabled": False, "Prefix": "!"},
    },
    "Console": {"Dashboard": True},
    "Auto_restart": {"Enabled": False, "Minutes": 60},
    "Update_check": False,
}

WEBHOOK_RE = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$"
)
ITEM_URL_RE = re.compile(r"roblox\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?catalog/(\d+)", re.I)


class ConfigError(Exception):
    pass


UGC, CLASSIC, AUTO = "ugc", "classic", "auto"  # AUTO = classic deal found via Rolimons values
DEAL_CALCULATIONS = ("value_or_rap", "value", "rap")


@dataclass(frozen=True)
class WatchItem:
    asset_id: int
    max_price: int
    buy_limit: int | None = None
    category: str = UGC
    min_price: int = 0
    ref_value: int | None = None  # Rolimons value (or RAP) the deal % is measured against

    def to_json(self) -> int | dict[str, int]:
        if self.buy_limit is None:
            return self.max_price
        return {"max_price": self.max_price, "buy_limit": self.buy_limit}


@dataclass(frozen=True)
class Settings:
    buy_cookie: str
    check_cookies: list[str]
    use_buy_cookie_for_checks: bool
    refresh_cookies_on_start: bool
    global_buy_limit: int | None
    dry_run: bool
    max_price_per_purchase: int
    daily_spend_limit: int
    robux_reserve: int
    watch_marketplace: bool
    watch_catalog: bool
    watch_economy: bool
    workers: dict[str, int]
    marketplace_batch_size: int
    catalog_batch_size: int
    lane_rates: dict[str, float]
    proxies_enabled: bool
    proxies_file: str
    proxies_only: bool
    proxies_send_check_cookie: bool
    proxy_max_failures: int
    tor_enabled: bool
    tor_path: str
    tor_instances: int
    tor_start_port: int
    webhook_enabled: bool
    webhook_url: str
    webhook_send_fails: bool
    webhook_ping_user: int
    discord_enabled: bool
    discord_token: str
    discord_owner_ids: list[int]
    discord_startup_message: bool
    discord_prefix_enabled: bool
    discord_prefix: str
    dashboard: bool
    auto_restart_minutes: int
    update_check: bool
    auto_deals_enabled: bool = False
    only_valued_items: bool = True
    skip_known_projecteds: bool = True
    min_deal_percent: float = 26
    ignore_prices_above: int = 1000000
    ignore_prices_below: int = 1000
    deal_calculation: str = "value_or_rap"
    rolimons_refresh_minutes: float = 2
    auto_exclude_ids: frozenset[int] = frozenset()
    items: dict[int, WatchItem] = field(default_factory=dict)  # manual UGC + classic items


def parse_item_ref(text: str | int) -> int:
    """Accept an asset id or a roblox.com catalog link and return the asset id."""
    s = str(text).strip()
    if s.isdigit():
        value = int(s)
    else:
        m = ITEM_URL_RE.search(s)
        if not m:
            raise ValueError(f"'{s}' is not an item id or roblox.com/catalog link")
        value = int(m.group(1))
    if value <= 0:
        raise ValueError("item id must be positive")
    return value


def parse_items(raw: Any, category: str = UGC) -> dict[int, WatchItem]:
    if raw is None:
        return {}
    if isinstance(raw, list):  # very old format: list of ids without prices
        raise ConfigError('"Items" must be an object of {"item id": max price}')
    if not isinstance(raw, dict):
        raise ConfigError('"Items" must be an object of {"item id": max price}')
    items: dict[int, WatchItem] = {}
    for key, value in raw.items():
        try:
            asset_id = parse_item_ref(key)
        except ValueError as e:
            raise ConfigError(f"Items: {e}") from None
        if isinstance(value, bool):
            raise ConfigError(f"Items.{key}: price must be a number")
        if isinstance(value, int):
            max_price, limit = value, None
        elif isinstance(value, dict):
            max_price = value.get("max_price")
            limit = value.get("buy_limit")
            if not isinstance(max_price, int) or isinstance(max_price, bool):
                raise ConfigError(f"Items.{key}.max_price must be a whole number")
            if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
                raise ConfigError(f"Items.{key}.buy_limit must be a whole number >= 1 or null")
        else:
            raise ConfigError(f"Items.{key}: expected a price or {{\"max_price\": .., \"buy_limit\": ..}}")
        if max_price < 0:
            raise ConfigError(f"Items.{key}: price can not be negative")
        if asset_id in items:
            raise ConfigError(f"Items.{key} is listed twice")
        items[asset_id] = WatchItem(asset_id, max_price, limit, category)
    return items


def _merge_defaults(defaults: dict, user: dict) -> dict:
    out = copy.deepcopy(user)
    for k, v in defaults.items():
        if k not in out:
            out[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(out[k], dict) and k != "Items":
            out[k] = _merge_defaults(v, out[k])
    return out


def migrate_legacy(raw: dict) -> dict:
    """Translate config.json files from the old v1.x sniper to the new layout."""
    raw = copy.deepcopy(raw)
    cookies = raw.get("Cookies", {})
    if "Check_cookie" in cookies:
        old = cookies.pop("Check_cookie")
        if old and old != cookies.get("Buy_cookie"):
            cookies.setdefault("Check_cookies", []).append(old)
        elif old:
            cookies["Use_buy_cookie_for_checks"] = True
    if "Bypass_cookies" in cookies:
        cookies["Refresh_on_start"] = bool(cookies.pop("Bypass_cookies"))
    if "Auto-Restart" in raw:
        old = raw.pop("Auto-Restart")
        raw["Auto_restart"] = {
            "Enabled": bool(old.get("Enabled")),
            "Minutes": max(1, int(old.get("Seconds", 3600)) // 60),
        }
    webhook = raw.get("Webhook", {})
    if "Send_Fails" in webhook:
        webhook["Send_fails"] = webhook.pop("Send_Fails")
    proxies = raw.get("Proxies", {})
    if "Amount" in proxies or "Max_Threads" in proxies:
        amount = proxies.pop("Amount", 10)
        proxies.pop("Max_Threads", None)
        proxies.setdefault("Tor", {})["Instances"] = amount
    discord = raw.get("Discord", {})
    if "Owner_id" in discord:
        discord["Owner_ids"] = discord.pop("Owner_id")
    if "Use_Prefix" in discord:
        discord["Prefix"] = discord.pop("Use_Prefix")
    if "StartUp_Message" in discord:
        discord["Startup_message"] = discord.pop("StartUp_Message")
    raw.pop("Global_Logs", None)  # the old hidden "global logs" no longer exist
    if "Items" in raw:  # before v2.1 there was one "Items" list; it moves to UGC
        old_items = raw.pop("Items") or {}
        raw.setdefault("UGC", {}).setdefault("Items", {})
        if isinstance(old_items, dict):
            raw["UGC"]["Items"] = {**old_items, **raw["UGC"]["Items"]}
    watchers = raw.get("Watchers", {})
    watchers.pop("Requests_per_second_per_lane", None)  # v2.0.0 -> per-endpoint Rate_per_lane
    watchers.pop("Workers_per_watcher", None)
    return raw


def _int(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ConfigError(f"{name} must be a whole number")
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return int(value)


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def build_settings(raw: dict) -> Settings:
    c, s, w = raw["Cookies"], raw["Safety"], raw["Watchers"]
    p, t, wh, d = raw["Proxies"], raw["Proxies"]["Tor"], raw["Webhook"], raw["Discord"]

    check = c.get("Check_cookies") or []
    if isinstance(check, str):
        check = [check]
    if not isinstance(check, list) or not all(isinstance(x, str) for x in check):
        raise ConfigError("Cookies.Check_cookies must be a list of strings")

    gbl = raw["Global_buy_limit"]
    owners = d.get("Owner_ids") or []
    if not isinstance(owners, list):
        raise ConfigError("Discord.Owner_ids must be a list of user ids")
    owner_ids = [_int(x, "Discord.Owner_ids[]", 1) for x in owners]

    webhook_url = (wh.get("Url") or "").strip()
    webhook_enabled = _bool(wh["Enabled"], "Webhook.Enabled")
    if webhook_enabled and not WEBHOOK_RE.match(webhook_url):
        raise ConfigError("Webhook.Url must be a Discord webhook URL (https://discord.com/api/webhooks/...)")

    rates: dict[str, float] = {}
    for k, v in w["Rate_per_lane"].items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.1 <= v <= 100:
            raise ConfigError(f"Watchers.Rate_per_lane.{k} must be between 0.1 and 100 requests per second")
        rates[k.lower()] = float(v)
    workers = {k.lower(): min(_int(v, f"Watchers.Workers.{k}", 1), 64) for k, v in w["Workers"].items()}

    return Settings(
        buy_cookie=str(c.get("Buy_cookie") or ""),
        check_cookies=[x for x in check if x.strip()],
        use_buy_cookie_for_checks=_bool(c["Use_buy_cookie_for_checks"], "Cookies.Use_buy_cookie_for_checks"),
        refresh_cookies_on_start=_bool(c["Refresh_on_start"], "Cookies.Refresh_on_start"),
        global_buy_limit=_int(gbl["Limit"], "Global_buy_limit.Limit", 1) if _bool(gbl["Enabled"], "Global_buy_limit.Enabled") else None,
        dry_run=_bool(s["Dry_run"], "Safety.Dry_run"),
        max_price_per_purchase=_int(s["Max_price_per_purchase"], "Safety.Max_price_per_purchase"),
        daily_spend_limit=_int(s["Daily_spend_limit"], "Safety.Daily_spend_limit"),
        robux_reserve=_int(s["Keep_robux_reserve"], "Safety.Keep_robux_reserve"),
        watch_marketplace=_bool(w["Marketplace_batch"], "Watchers.Marketplace_batch"),
        watch_catalog=_bool(w["Catalog_batch"], "Watchers.Catalog_batch"),
        watch_economy=_bool(w["Economy_single"], "Watchers.Economy_single"),
        workers=workers,
        marketplace_batch_size=min(_int(w["Marketplace_batch_size"], "Watchers.Marketplace_batch_size", 1), 120),
        catalog_batch_size=min(_int(w["Catalog_batch_size"], "Watchers.Catalog_batch_size", 1), 120),
        lane_rates=rates,
        proxies_enabled=_bool(p["Enabled"], "Proxies.Enabled"),
        proxies_file=str(p.get("File") or "proxies.txt"),
        proxies_only=_bool(p["Proxies_only"], "Proxies.Proxies_only"),
        proxies_send_check_cookie=_bool(p["Send_check_cookie"], "Proxies.Send_check_cookie"),
        proxy_max_failures=_int(p["Max_failures"], "Proxies.Max_failures", 1),
        tor_enabled=_bool(t["Enabled"], "Proxies.Tor.Enabled"),
        tor_path=str(t.get("Path") or ""),
        tor_instances=min(_int(t["Instances"], "Proxies.Tor.Instances", 1), 200),
        tor_start_port=_int(t["Start_port"], "Proxies.Tor.Start_port", 1024),
        webhook_enabled=webhook_enabled,
        webhook_url=webhook_url,
        webhook_send_fails=_bool(wh["Send_fails"], "Webhook.Send_fails"),
        webhook_ping_user=_int(wh.get("UserID_To_Ping") or 0, "Webhook.UserID_To_Ping"),
        discord_enabled=_bool(d["Enabled"], "Discord.Enabled"),
        discord_token=str(d.get("Token") or ""),
        discord_owner_ids=owner_ids,
        discord_startup_message=_bool(d["Startup_message"], "Discord.Startup_message"),
        discord_prefix_enabled=_bool(d["Prefix"]["Enabled"], "Discord.Prefix.Enabled"),
        discord_prefix=str(d["Prefix"].get("Prefix") or "!"),
        dashboard=_bool(raw["Console"]["Dashboard"], "Console.Dashboard"),
        auto_restart_minutes=_int(raw["Auto_restart"]["Minutes"], "Auto_restart.Minutes", 1)
        if _bool(raw["Auto_restart"]["Enabled"], "Auto_restart.Enabled") else 0,
        update_check=_bool(raw["Update_check"], "Update_check"),
        **_classic_settings(raw),
    )


def _classic_settings(raw: dict) -> dict:
    ugc = parse_items(raw["UGC"].get("Items"), UGC)
    classic = parse_items(raw["Classic"].get("Items"), CLASSIC)
    both = set(ugc) & set(classic)
    if both:
        raise ConfigError(f"item {min(both)} is in both UGC.Items and Classic.Items")
    a = raw["Classic"]["Auto_deals"]
    pct = a["Min_deal_percent"]
    if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 <= pct < 100:
        raise ConfigError("Classic.Auto_deals.Min_deal_percent must be between 0 and 99")
    calc = str(a["Deal_calculation"]).lower().replace("/", "_or_").replace("(", "").replace(")", "")
    if calc not in DEAL_CALCULATIONS:
        raise ConfigError(f"Classic.Auto_deals.Deal_calculation must be one of {', '.join(DEAL_CALCULATIONS)}")
    refresh = a["Refresh_minutes"]
    if isinstance(refresh, bool) or not isinstance(refresh, (int, float)) or refresh < 1:
        raise ConfigError("Classic.Auto_deals.Refresh_minutes must be at least 1")
    lo = _int(a["Ignore_prices_below"], "Classic.Auto_deals.Ignore_prices_below")
    hi = _int(a["Ignore_prices_above"], "Classic.Auto_deals.Ignore_prices_above", 1)
    if lo > hi:
        raise ConfigError("Classic.Auto_deals.Ignore_prices_below is higher than Ignore_prices_above")
    excl = a.get("Exclude_ids") or []
    if not isinstance(excl, list):
        raise ConfigError("Classic.Auto_deals.Exclude_ids must be a list of item ids")
    return dict(
        auto_deals_enabled=_bool(a["Enabled"], "Classic.Auto_deals.Enabled"),
        only_valued_items=_bool(a["Only_valued_items"], "Classic.Auto_deals.Only_valued_items"),
        skip_known_projecteds=_bool(a["Skip_known_projecteds"], "Classic.Auto_deals.Skip_known_projecteds"),
        min_deal_percent=float(pct),
        ignore_prices_above=hi,
        ignore_prices_below=lo,
        deal_calculation=calc,
        rolimons_refresh_minutes=float(refresh),
        auto_exclude_ids=frozenset(_int(x, "Classic.Auto_deals.Exclude_ids[]", 1) for x in excl),
        items={**ugc, **classic},
    )


class ConfigStore:
    """Owns config.json. All writes are atomic and serialized so the Discord bot,
    the console and a hand edit in a text editor can never corrupt the file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.raw: dict = {}
        self.settings: Settings | None = None
        self._mtime = 0.0

    def load(self) -> Settings:
        with self._lock:
            try:
                text = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                raise ConfigError(f"{self.path} not found") from None
            try:
                user = json.loads(text)
            except json.JSONDecodeError as e:
                raise ConfigError(f"{self.path} is not valid JSON: line {e.lineno}, column {e.colno}: {e.msg}") from None
            if not isinstance(user, dict):
                raise ConfigError(f"{self.path} must contain a JSON object")
            raw = _merge_defaults(DEFAULTS, migrate_legacy(user))
            settings = build_settings(raw)
            self.raw, self.settings = raw, settings
            self._mtime = self.path.stat().st_mtime
            return settings

    def mark_seen(self) -> None:
        """Accept the current file on disk as seen (even if it failed to load)."""
        try:
            self._mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            pass

    def changed_on_disk(self) -> bool:
        try:
            return self.path.stat().st_mtime != self._mtime
        except FileNotFoundError:
            return False

    def save(self) -> None:
        with self._lock:
            data = json.dumps(self.raw, indent=4, ensure_ascii=False) + "\n"
            fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=self.path.parent or ".")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._mtime = self.path.stat().st_mtime

    # --- item mutations (used by console and Discord commands) ---------------

    def _write_items(self, items: dict[int, WatchItem]) -> None:
        self.raw["UGC"]["Items"] = {str(k): v.to_json() for k, v in items.items() if v.category == UGC}
        self.raw["Classic"]["Items"] = {str(k): v.to_json() for k, v in items.items() if v.category == CLASSIC}
        self.settings = build_settings(self.raw)
        self.save()

    def set_items(self, items: dict[int, WatchItem]) -> None:
        with self._lock:
            self._write_items(items)

    def update_setting(self, section: str, key: str, value: Any) -> None:
        """section may be nested with dots, e.g. "Classic.Auto_deals"."""
        with self._lock:
            target = self.raw
            for part in section.split("."):
                target = target[part]
            old = target[key]
            target[key] = value
            try:
                self.settings = build_settings(self.raw)
            except ConfigError:
                target[key] = old
                raise
            self.save()
