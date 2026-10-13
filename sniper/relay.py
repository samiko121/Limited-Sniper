"""HTTP relays (e.g. a small Vercel / Deno function) used as extra checking lanes.

A relay receives the request at its own URL with two headers,
    x-relay-target: https://economy.roblox.com
    x-relay-path:   /v2/assets/1028606/details
and forwards it. Only anonymous price checks go through relays: cookies are
stripped here no matter what, and purchases never use a lane.

Note: Roblox blocks many datacenter / serverless IP ranges (Vercel returns
"403 Request forbidden by administrative rules"), so every relay is tested at
startup and skipped if Roblox refuses it.
"""

from __future__ import annotations

import httpx

HOP_HEADERS = ("cookie", "authorization")


class RelayTransport(httpx.AsyncBaseTransport):
    def __init__(self, relay_url: str):
        if not relay_url.startswith("https://"):
            raise ValueError(f"relay must be an https:// URL: {relay_url}")
        self.relay = httpx.URL(relay_url.rstrip("/") + "/")
        self._inner = httpx.AsyncHTTPTransport(http2=True, retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        target = f"{request.url.scheme}://{request.url.host}"
        path = request.url.raw_path.decode("ascii")
        headers = [(k, v) for k, v in request.headers.raw if k.decode().lower() not in HOP_HEADERS + ("host",)]
        headers += [(b"host", self.relay.netloc), (b"x-relay-target", target.encode()), (b"x-relay-path", path.encode())]
        relayed = httpx.Request(request.method, self.relay, headers=headers, stream=request.stream,
                                extensions=request.extensions)
        return await self._inner.handle_async_request(relayed)

    async def aclose(self) -> None:
        await self._inner.aclose()


def relay_label(url: str) -> str:
    host = httpx.URL(url).host
    return f"relay {host[:40]}"
