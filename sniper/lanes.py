"""Checking "lanes": one identity (alt cookie and/or proxy) with its own rate limit.

Watchers ask the pool for a lane before every request. Each lane has a token
bucket; when Roblox answers 429 the lane halves its speed and cools down, then
slowly speeds back up (AIMD). Proxies that keep failing are benched.
Purchases never go through a lane - they always use the buy account directly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from urllib.parse import quote

from .roblox import RobloxSession

log = logging.getLogger(__name__)

MIN_RPS = 0.2


class Lane:
    def __init__(self, session: RobloxSession, rps: float, max_failures: int = 5, label: str = ""):
        self.session = session
        self.label = label or session.name
        self.max_rps = rps
        self.rps = rps
        self.max_failures = max_failures
        self.next_at = 0.0
        self.cooldown_until = 0.0
        self.failures = 0
        self.benched_until = 0.0
        self.requests = 0
        self.rate_limits = 0
        self.latency = 0.0

    def ready_at(self) -> float:
        return max(self.next_at, self.cooldown_until, self.benched_until)

    def take(self, now: float) -> None:
        start = max(now, self.next_at)
        self.next_at = start + 1.0 / self.rps
        self.requests += 1

    def ok(self, latency: float) -> None:
        self.failures = 0
        self.latency = latency if not self.latency else self.latency * 0.8 + latency * 0.2
        self.rps = min(self.max_rps, self.rps + 0.05 * self.max_rps)

    def rate_limited(self) -> None:
        self.rate_limits += 1
        self.rps = max(MIN_RPS, self.rps / 2)
        self.cooldown_until = time.monotonic() + min(30.0, 2.0 * (self.max_rps / self.rps))

    def failed(self) -> None:
        self.failures += 1
        if self.failures >= self.max_failures:
            backoff = min(300.0, 15.0 * 2 ** (self.failures - self.max_failures))
            self.benched_until = time.monotonic() + backoff
            log.warning("Lane %s benched for %.0fs after %d failures", self.label, backoff, self.failures)

    @property
    def status(self) -> str:
        now = time.monotonic()
        if self.benched_until > now:
            return "benched"
        if self.cooldown_until > now:
            return "cooldown"
        return "ok"


class LanePool:
    def __init__(self, lanes: list[Lane]):
        if not lanes:
            raise ValueError("at least one lane is required")
        self.lanes = lanes
        self._lock = asyncio.Lock()

    async def acquire(self) -> Lane:
        """Wait for the lane that becomes available first and reserve a slot on it."""
        while True:
            async with self._lock:
                now = time.monotonic()
                lane = min(self.lanes, key=lambda l: l.ready_at())
                wait = lane.ready_at() - now
                if wait <= 0:
                    lane.take(now)
                    return lane
            await asyncio.sleep(min(wait, 1.0))

    async def aclose(self) -> None:
        await asyncio.gather(*(l.session.aclose() for l in self.lanes), return_exceptions=True)


def parse_proxy(line: str) -> str | None:
    """Accepts host:port, host:port:user:pass, user:pass@host:port or a full
    http:// / https:// / socks5:// URL. Returns a proxy URL or None."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if "://" in s:
        scheme = s.split("://", 1)[0].lower()
        if scheme not in ("http", "https", "socks5", "socks5h"):
            raise ValueError(f"unsupported proxy scheme '{scheme}'")
        return s
    parts = s.split(":")
    if len(parts) >= 4 and parts[1].isdigit():  # host:port:user:pass (pass may contain ':' or '@')
        host, port, user, pw = parts[0], parts[1], parts[2], ":".join(parts[3:])
        return f"http://{quote(user, safe='')}:{quote(pw, safe='')}@{host}:{port}"
    if "@" in s:
        return f"http://{s}"
    if len(parts) == 2 and parts[1].isdigit():
        return f"http://{parts[0]}:{parts[1]}"
    raise ValueError(f"can not parse proxy '{s}'")


def load_proxies(path: str | Path) -> list[str]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"{p} not found (one proxy per line)")
    out = []
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        try:
            url = parse_proxy(line)
        except ValueError as e:
            log.warning("%s line %d: %s", p.name, n, e)
            continue
        if url:
            out.append(url)
    return out


def proxy_label(url: str) -> str:
    """host:port without credentials, for display."""
    rest = url.split("://", 1)[-1]
    return rest.rsplit("@", 1)[-1]
