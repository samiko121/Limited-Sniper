import asyncio
import logging
import re
import time
from pathlib import Path

import pytest

from sniper import secrets
from sniper.lanes import Lane, LanePool, parse_proxy, proxy_label
from sniper.roblox import RobloxSession
from sniper.state import Watchlist
from sniper.config import WatchItem

from .conftest import COOKIE

SRC = Path(__file__).resolve().parent.parent / "sniper"


def test_no_telemetry_hosts_in_source():
    """The only hosts hard-coded anywhere are Roblox, GitHub (opt-in update
    check of this repo) and links shown to the user. Anything else fails."""
    allowed = re.compile(
        r"^https://(?:[a-z]+\.)?roblox\.com\b"
        r"|^https://raw\.githubusercontent\.com/samiko121/Limited-Sniper/"
        r"|^https://www\.rolimons\.com/itemapi/itemdetails$"  # public classic limited values (GET only)
        r"|^https://www\.torproject\.org/"
        r"|^https://discord\.com/api/webhooks/\.\.\."  # config error message
        r"|^https://www\.python\.org/"
        r"|^https?://\{"                 # built from the user's own proxies.txt
        r"|^http://127\.0\.0\.1:"         # local Tor
    )
    found = []
    for f in SRC.glob("*.py"):
        for url in re.findall(r"https?://[^\s\"'`)]+", f.read_text(encoding="utf-8")):
            if "(?" in url:  # a regex pattern, not a request
                continue
            if not allowed.match(url):
                found.append(f"{f.name}: {url}")
    assert found == []


def test_no_dynamic_code_execution():
    for f in SRC.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"(?<![\w.])(eval|exec|compile)\(", text), f.name
        for bad in ("marshal", "__import__", "base64", "zlib", "pickle"):
            assert bad not in text, f"{bad} in {f.name}"


def test_redaction():
    secrets.register("my-discord-token-123")
    msg = f"failed with cookie {COOKIE} and token my-discord-token-123"
    out = secrets.mask(msg)
    assert "TESTCOOKIE" not in out and "my-discord-token-123" not in out

    record = logging.LogRecord("x", logging.INFO, "", 0, "cookie=%s", (COOKIE,), None)
    secrets.RedactingFilter().filter(record)
    assert "TESTCOOKIE" not in record.getMessage()


def test_secret_env(monkeypatch):
    monkeypatch.setenv("LS_TEST_COOKIE", "value-from-env-123")
    assert secrets.resolve("env:LS_TEST_COOKIE", "x") == "value-from-env-123"
    with pytest.raises(secrets.SecretError):
        secrets.resolve("env:LS_MISSING_VAR", "x")


@pytest.mark.parametrize("line,url", [
    ("1.2.3.4:8080", "http://1.2.3.4:8080"),
    ("1.2.3.4:8080:user:p@ss", "http://user:p%40ss@1.2.3.4:8080"),
    ("user:pw@1.2.3.4:8080", "http://user:pw@1.2.3.4:8080"),
    ("socks5://u:p@h:1080", "socks5://u:p@h:1080"),
    ("# comment", None),
    ("", None),
])
def test_parse_proxy(line, url):
    assert parse_proxy(line) == url


def test_proxy_label_hides_credentials():
    assert proxy_label("http://user:secret@1.2.3.4:80") == "1.2.3.4:80"


def test_lane_aimd_per_endpoint_and_bench():
    lane = Lane(RobloxSession(None), {"marketplace": 8, "catalog": 2}, max_failures=2)
    lane.rate_limited("catalog")
    assert lane.bucket("catalog").rps == 1 and lane.status == "slow:catalog"
    assert lane.bucket("marketplace").rps == 8  # other endpoints are not slowed down
    assert lane.ready_at("marketplace") <= time.monotonic()
    lane.ok("catalog", 0.1)
    assert 1 < lane.bucket("catalog").rps <= 2
    lane.failed()
    lane.failed()
    assert lane.status == "benched"
    asyncio.run(lane.session.aclose())


def test_pool_respects_rate():
    async def go():
        pool = LanePool([Lane(RobloxSession(None), 20)])
        t0 = time.monotonic()
        for _ in range(5):
            await pool.acquire("economy")
        took = time.monotonic() - t0
        await pool.aclose()
        return took

    assert asyncio.run(go()) >= 0.15  # 5 slots at 20/s


def test_watchlist_round_robin_and_focus():
    wl = Watchlist()
    wl.set_items({i: WatchItem(i, 1) for i in range(1, 6)})
    assert wl.next_batch("a", 2) == [1, 2]
    assert wl.next_batch("a", 2) == [3, 4]
    assert wl.next_batch("a", 2) == [5, 1]
    assert wl.next_batch("b", 10) == [1, 2, 3, 4, 5]
    wl.focus = WatchItem(99, 7)
    assert wl.next_batch("a", 5) == [99]
    assert wl.get(1) is None and wl.get(99).max_price == 7


def test_relay_transport_rewrites_and_strips_cookies():
    import httpx
    from sniper.relay import RelayTransport
    seen = {}

    class Inner(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            seen["url"] = str(request.url)
            seen["headers"] = dict(request.headers)
            return httpx.Response(200, json={"ok": True})

    t = RelayTransport("https://relay.example.app")
    t._inner = Inner()

    async def go():
        async with httpx.AsyncClient(transport=t) as c:
            return await c.get("https://economy.roblox.com/v2/assets/5/details?x=1", headers={"Cookie": ".ROBLOSECURITY=secret"})

    r = asyncio.run(go())
    assert r.json() == {"ok": True}
    assert seen["url"] == "https://relay.example.app/"
    assert seen["headers"]["x-relay-target"] == "https://economy.roblox.com"
    assert seen["headers"]["x-relay-path"] == "/v2/assets/5/details?x=1"
    assert "cookie" not in seen["headers"]
