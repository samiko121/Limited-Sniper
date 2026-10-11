"""Startup, command line and shutdown."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import logging.handlers
import os
import shutil
import sys
from pathlib import Path

import httpx

from . import __version__, roblox, secrets
from .commands import Commands
from .config import ConfigError, ConfigStore, Settings
from .engine import Engine
from .history import History
from .lanes import Lane, LanePool, load_proxies, proxy_label
from .notify import Notifier
from .tor import TorProcess

log = logging.getLogger("sniper")

ROOT = Path(__file__).resolve().parent.parent
UPDATE_URL = "https://raw.githubusercontent.com/samiko121/Limited-Sniper/main/VERSION"


def setup_logging(dashboard: bool, debug: bool) -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    redact = secrets.RedactingFilter()
    fh = logging.handlers.RotatingFileHandler(ROOT / "logs" / "sniper.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    fh.addFilter(redact)
    root.addHandler(fh)
    if not dashboard:
        from rich.logging import RichHandler
        ch = RichHandler(show_path=False, markup=False, rich_tracebacks=False)
        ch.addFilter(redact)
        root.addHandler(ch)
    for noisy in ("httpx", "httpcore", "hpack", "discord", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_lanes(s: Settings, check_cookies: list[str], buy_cookie: str, proxies: list[str]) -> LanePool:
    lanes: list[Lane] = []
    cookies = list(check_cookies) or ([buy_cookie] if s.use_buy_cookie_for_checks else [])
    if not (s.proxies_enabled and s.proxies_only):
        if cookies:
            for i, ck in enumerate(cookies):
                lanes.append(Lane(roblox.RobloxSession(ck, name=f"alt-{i + 1}"), s.lane_rps, s.proxy_max_failures))
        else:
            lanes.append(Lane(roblox.RobloxSession(None, name="direct"), s.lane_rps, s.proxy_max_failures))
    for i, url in enumerate(proxies):
        ck = cookies[i % len(cookies)] if (cookies and s.proxies_send_check_cookie) else None
        session = roblox.RobloxSession(ck, proxy=url, http2=False, name=f"proxy {proxy_label(url)}")
        lanes.append(Lane(session, s.lane_rps, s.proxy_max_failures))
    return LanePool(lanes)


async def check_update() -> None:
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as c:
            r = await c.get(UPDATE_URL)
        latest = r.text.strip()
        if r.status_code == 200 and latest and latest != __version__:
            log.warning("A new version is available: %s (you have %s). Run: git pull", latest, __version__)
    except httpx.HTTPError:
        pass


async def run(config_path: Path, args: argparse.Namespace) -> bool:
    """Run until stopped. Returns True if a restart was requested."""
    store = ConfigStore(config_path)
    s = store.load()
    dashboard = s.dashboard and not args.no_dashboard and sys.stdout.isatty()
    setup_logging(dashboard, args.debug)
    if args.dry_run and not s.dry_run:
        store.update_setting("Safety", "Dry_run", True)
        s = store.settings

    buy_cookie = secrets.resolve(s.buy_cookie, "Cookies.Buy_cookie")
    if not buy_cookie:
        raise ConfigError("Cookies.Buy_cookie is empty")
    check_cookies = [c for c in (secrets.resolve(x, "Cookies.Check_cookies") for x in s.check_cookies) if c]
    discord_token = secrets.resolve(s.discord_token, "Discord.Token") if s.discord_enabled else None

    if s.refresh_cookies_on_start:
        log.info("Refreshing cookies for this IP...")
        tmp = roblox.RobloxSession(buy_cookie)
        try:
            buy_cookie = await roblox.refresh_cookie(tmp)
            secrets.register(buy_cookie)
        finally:
            await tmp.aclose()

    tor: TorProcess | None = None
    proxies: list[str] = []
    if s.proxies_enabled:
        if s.tor_enabled:
            tor = TorProcess(s.tor_path, s.tor_instances, s.tor_start_port)
            log.info("Starting Tor...")
            proxies += await tor.start()
        pfile = Path(s.proxies_file)
        if not pfile.is_absolute():
            pfile = config_path.parent / pfile
        if pfile.exists():
            proxies += load_proxies(pfile)
        elif not s.tor_enabled:
            raise ConfigError(f"Proxies are enabled but {pfile} does not exist")
        if not proxies:
            raise ConfigError("Proxies are enabled but no proxies were loaded")
        log.info("Loaded %d proxies", len(proxies))

    buy_session = roblox.RobloxSession(buy_cookie, name="buyer")
    lanes = build_lanes(s, check_cookies, buy_cookie, proxies)
    history = History(ROOT / "data" / "history.db")
    notifier = Notifier(s.webhook_url if s.webhook_enabled else None, s.webhook_send_fails, s.webhook_ping_user)
    engine = Engine(store, buy_session, lanes, history, notifier)
    cmds = Commands(engine)
    ui_tasks: list[asyncio.Task] = []
    try:
        try:
            await engine.login()
        except roblox.Unauthorized:
            raise ConfigError("Buy cookie is invalid or expired (Roblox answered 401)") from None
        for lane in lanes.lanes:
            if lane.session.has_cookie:
                try:
                    _, name = await roblox.get_authenticated_user(lane.session)
                    lane.label = f"{lane.label} ({name})"
                except roblox.Unauthorized:
                    log.warning("Check cookie for %s is invalid - that lane will check anonymously", lane.label)
                except (roblox.RobloxHTTPError, httpx.HTTPError) as e:
                    log.warning("Could not verify %s: %s", lane.label, e)
        engine.start()
        if s.update_check:
            asyncio.get_running_loop().create_task(check_update())

        from .console import ConsoleUI
        ui = ConsoleUI(engine, cmds, dashboard)
        ui_tasks.append(asyncio.get_running_loop().create_task(ui.run(), name="console"))
        if s.discord_enabled and discord_token:
            from .discord_bot import run_bot
            ui_tasks.append(asyncio.get_running_loop().create_task(run_bot(engine, cmds, discord_token), name="discord"))

        await engine.stop_event.wait()
    finally:
        for t in ui_tasks:
            t.cancel()
        await asyncio.gather(*ui_tasks, return_exceptions=True)
        await engine.stop()
        await notifier.aclose()
        await lanes.aclose()
        await buy_session.aclose()
        history.close()
        if tor:
            await tor.stop()
    return engine.restart_requested


async def selftest(config_path: Path) -> int:
    """Check cookies and endpoints without buying anything."""
    store = ConfigStore(config_path)
    s = store.load()
    ok = True

    def report(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"  [{'OK' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))

    print(f"Limited Sniper v{__version__} self test")
    buy_cookie = secrets.resolve(s.buy_cookie, "Cookies.Buy_cookie")
    anon = roblox.RobloxSession(None)
    buyer = roblox.RobloxSession(buy_cookie) if buy_cookie else None
    try:
        if buyer:
            try:
                uid, name = await roblox.get_authenticated_user(buyer)
                robux = await roblox.get_robux(buyer, uid)
                report("buy cookie", True, f"{name} ({uid}), {robux:,} R$")
            except Exception as e:
                report("buy cookie", False, str(e))
        else:
            report("buy cookie", False, "Cookies.Buy_cookie is empty")
        for i, raw in enumerate(s.check_cookies, 1):
            sess = roblox.RobloxSession(secrets.resolve(raw, "Check cookie"))
            try:
                _, name = await roblox.get_authenticated_user(sess)
                report(f"check cookie {i}", True, name)
            except Exception as e:
                report(f"check cookie {i}", False, str(e))
            finally:
                await sess.aclose()

        sample = next(iter(s.items), 1028606)
        try:
            d = await roblox.economy_details(anon, sample)
            report("economy details", True, d.get("Name", "?"))
            cid = d.get("CollectibleItemId")
        except Exception as e:
            report("economy details", False, str(e))
            cid = None
        try:
            rows = await roblox.catalog_details(anon, [sample])
            report("catalog batch", bool(rows), f"{len(rows)} row(s)")
        except Exception as e:
            report("catalog batch", False, str(e))
        if cid:
            try:
                rows = await roblox.marketplace_details(anon, [cid])
                report("marketplace batch", bool(rows), f"{len(rows)} row(s)")
            except Exception as e:
                report("marketplace batch", False, str(e))
            try:
                row = await roblox.lowest_reseller(anon, cid)
                report("resellers", True, f"lowest {row['price']} R$" if row else "no resellers")
            except Exception as e:
                report("resellers", False, str(e))
        if s.proxies_enabled and not s.tor_enabled:
            pfile = Path(s.proxies_file)
            if not pfile.is_absolute():
                pfile = config_path.parent / pfile
            for url in load_proxies(pfile)[:5]:
                p = roblox.RobloxSession(None, proxy=url, http2=False)
                try:
                    await roblox.economy_details(p, sample)
                    report(f"proxy {proxy_label(url)}", True)
                except Exception as e:
                    report(f"proxy {proxy_label(url)}", False, type(e).__name__)
                finally:
                    await p.aclose()
    finally:
        await anon.aclose()
        if buyer:
            await buyer.aclose()
    print("All good." if ok else "Some checks failed.")
    return 0 if ok else 1


def set_secret(config_path: Path, name: str) -> int:
    targets = {"buy": ("Cookies", "Buy_cookie"), "discord": ("Discord", "Token")}
    secret = getpass.getpass(f"Paste the value for '{name}' (input is hidden): ").strip()
    if not secret:
        print("Nothing entered.")
        return 1
    try:
        secrets.store(name, secret)
    except Exception as e:
        print(f"Could not use the OS credential store: {e}")
        return 1
    store = ConfigStore(config_path)
    store.load()
    ref = f"keyring:{name}"
    if name in targets:
        store.update_setting(*targets[name], ref)
        print(f"Stored. config.json now uses \"{ref}\".")
    else:
        checks = list(store.raw["Cookies"].get("Check_cookies") or [])
        if ref not in checks:
            checks.append(ref)
        store.update_setting("Cookies", "Check_cookies", checks)
        print(f"Stored as check cookie. config.json now lists \"{ref}\".")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="main.py", description="Limited Sniper")
    parser.add_argument("--config", default=str(ROOT / "config.json"), help="path to config.json")
    parser.add_argument("--dry-run", action="store_true", help="simulate purchases (also saved to config)")
    parser.add_argument("--no-dashboard", action="store_true", help="plain log output instead of the dashboard")
    parser.add_argument("--debug", action="store_true", help="verbose logging to logs/sniper.log")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="start sniping (default)")
    sub.add_parser("selftest", help="check cookies and Roblox endpoints without buying")
    ss = sub.add_parser("set-secret", help="store a cookie/token in the OS credential store")
    ss.add_argument("name", help="'buy', 'discord', or any name for a check cookie (e.g. alt1)")
    args = parser.parse_args(argv)

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        example = ROOT / "config.example.json"
        shutil.copy(example, config_path)
        print(f"Created {config_path.name} from config.example.json. Fill in your cookie and items, then start again.")
        sys.exit(1)

    try:
        if args.cmd == "set-secret":
            sys.exit(set_secret(config_path, args.name))
        if args.cmd == "selftest":
            sys.exit(asyncio.run(selftest(config_path)))
        restart = asyncio.run(run(config_path, args))
    except ConfigError as e:
        print(f"Config error: {e}")
        sys.exit(2)
    except secrets.SecretError as e:
        print(f"Secret error: {e}")
        sys.exit(2)
    except KeyboardInterrupt:
        print("Stopped.")
        sys.exit(0)

    if restart:
        print("Restarting...")
        os.execv(sys.executable, [sys.executable, *sys.argv])
