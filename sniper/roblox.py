"""Thin async client for the Roblox web APIs the sniper needs.

Every request goes to a *.roblox.com host over verified HTTPS. CSRF tokens are
handled transparently: a 403 that carries a fresh x-csrf-token is retried once
(Roblox rejects the request before doing anything, so the retry is safe even
for purchases, and the idempotency key stays the same).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .models import RESALE, STOCK, Offer, PurchaseResult

log = logging.getLogger(__name__)

APIS = "https://apis.roblox.com"
ECONOMY = "https://economy.roblox.com"
CATALOG = "https://catalog.roblox.com"
USERS = "https://users.roblox.com"
AUTH = "https://auth.roblox.com"

CATALOG_DETAILS = f"{CATALOG}/v1/catalog/items/details"
MARKETPLACE_DETAILS = f"{APIS}/marketplace-items/v1/items/details"


class RobloxHTTPError(Exception):
    def __init__(self, status: int, body: str, url: str = ""):
        super().__init__(f"HTTP {status} from {url}: {body[:200]}")
        self.status = status
        self.body = body


class RateLimited(RobloxHTTPError):
    pass


class Unauthorized(RobloxHTTPError):
    pass


class RobloxSession:
    """One HTTP identity: an optional cookie and an optional proxy."""

    def __init__(
        self,
        cookie: str | None = None,
        proxy: str | None = None,
        *,
        http2: bool = True,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        name: str = "",
    ):
        cookies = httpx.Cookies()
        if cookie:
            cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")
        self.name = name
        self.proxy = proxy
        self.has_cookie = bool(cookie)
        self.csrf: str | None = None
        self.client = httpx.AsyncClient(
            http2=http2,
            proxy=proxy,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            cookies=cookies,
            transport=transport,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=32, keepalive_expiry=90),
            headers={"Accept": "application/json"},
            trust_env=False,  # never pick up proxies from the environment by surprise
        )

    @property
    def cookie(self) -> str | None:
        return self.client.cookies.get(".ROBLOSECURITY", domain=".roblox.com")

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        mutating = method.upper() != "GET"
        extra = kwargs.pop("headers", {}) or {}
        for attempt in (0, 1):
            headers = dict(extra)
            if mutating and self.csrf:
                headers["x-csrf-token"] = self.csrf
            r = await self.client.request(method, url, headers=headers, **kwargs)
            token = r.headers.get("x-csrf-token")
            if r.status_code == 403 and mutating and token and attempt == 0:
                self.csrf = token
                continue
            return r
        return r  # pragma: no cover

    async def refresh_csrf(self) -> None:
        r = await self.client.post(CATALOG_DETAILS, json={"items": []})
        token = r.headers.get("x-csrf-token")
        if token:
            self.csrf = token

    async def aclose(self) -> None:
        await self.client.aclose()


def _check(r: httpx.Response) -> Any:
    if r.status_code == 200:
        return r.json()
    url = str(r.request.url) if r.request else ""
    if r.status_code == 429:
        raise RateLimited(429, r.text, url)
    if r.status_code == 401:
        raise Unauthorized(401, r.text, url)
    raise RobloxHTTPError(r.status_code, r.text, url)


# --- account -----------------------------------------------------------------

async def get_authenticated_user(s: RobloxSession) -> tuple[int, str]:
    data = _check(await s.request("GET", f"{USERS}/v1/users/authenticated"))
    return int(data["id"]), str(data["name"])


async def get_robux(s: RobloxSession, user_id: int) -> int:
    data = _check(await s.request("GET", f"{ECONOMY}/v1/users/{user_id}/currency"))
    return int(data.get("robux", 0))


async def owned_count(s: RobloxSession, collectible_item_id: str, user_id: int) -> int:
    """How many copies of a collectible the user owns (follows pagination)."""
    count, cursor = 0, ""
    for _ in range(20):
        data = _check(await s.request(
            "GET",
            f"{APIS}/marketplace-sales/v1/item/{collectible_item_id}/resellable-instances",
            params={"cursor": cursor, "ownerType": "User", "ownerId": user_id, "limit": 500},
        ))
        count += len(data.get("itemInstances") or [])
        cursor = data.get("nextPageCursor") or ""
        if not cursor:
            break
    return count


async def latest_serial(s: RobloxSession, collectible_item_id: str, user_id: int) -> int | None:
    data = _check(await s.request(
        "GET",
        f"{APIS}/marketplace-sales/v1/item/{collectible_item_id}/resellable-instances",
        params={"cursor": "", "ownerType": "User", "ownerId": user_id, "limit": 500},
    ))
    serials = [i.get("serialNumber") for i in data.get("itemInstances") or [] if i.get("serialNumber")]
    return max(serials) if serials else None


async def refresh_cookie(s: RobloxSession) -> str:
    """Get a fresh .ROBLOSECURITY for this machine/IP via an authentication ticket.

    This is what the old sniper called "Bypass_cookies": it fixes cookies that were
    created in another region / on another IP. The old cookie stays valid.
    """
    await s.refresh_csrf()
    r = await s.request(
        "POST", f"{AUTH}/v1/authentication-ticket",
        headers={"rbxauthenticationnegotiation": "1", "Referer": "https://www.roblox.com/"},
        json={},
    )
    ticket = r.headers.get("rbx-authentication-ticket")
    if not ticket:
        raise RobloxHTTPError(r.status_code, "no authentication ticket returned (invalid cookie?)", str(r.request.url))
    async with httpx.AsyncClient(proxy=s.proxy, timeout=10, trust_env=False) as fresh:
        r = await fresh.post(
            f"{AUTH}/v1/authentication-ticket/redeem",
            headers={"rbxauthenticationnegotiation": "1"},
            json={"authenticationTicket": ticket},
        )
        cookie = r.cookies.get(".ROBLOSECURITY")
    if not cookie:
        raise RobloxHTTPError(r.status_code, "ticket redeem did not return a cookie", str(r.request.url))
    return cookie


# --- item data ---------------------------------------------------------------

async def economy_details(s: RobloxSession, asset_id: int) -> dict:
    return _check(await s.request("GET", f"{ECONOMY}/v2/assets/{asset_id}/details"))


async def catalog_details(s: RobloxSession, asset_ids: list[int]) -> list[dict]:
    data = _check(await s.request(
        "POST", CATALOG_DETAILS, json={"items": [{"itemType": "Asset", "id": i} for i in asset_ids]}
    ))
    return data.get("data") or []


async def marketplace_details(s: RobloxSession, collectible_ids: list[str]) -> list[dict]:
    data = _check(await s.request("POST", MARKETPLACE_DETAILS, json={"itemIds": collectible_ids}))
    return data if isinstance(data, list) else data.get("data") or []


async def lowest_reseller(s: RobloxSession, collectible_item_id: str) -> dict | None:
    data = _check(await s.request(
        "GET", f"{APIS}/marketplace-sales/v1/item/{collectible_item_id}/resellers", params={"limit": 1}
    ))
    rows = data.get("data") or []
    return rows[0] if rows else None


# --- purchasing --------------------------------------------------------------

def purchase_payload(offer: Offer, buyer_id: int, idempotency_key: str) -> dict:
    payload: dict[str, Any] = {
        "collectibleItemId": offer.collectible_item_id,
        "collectibleProductId": offer.collectible_product_id,
        "expectedCurrency": 1,
        "expectedPrice": offer.price,
        "expectedPurchaserId": buyer_id,
        "expectedPurchaserType": "User",
        "expectedSellerType": offer.seller_type or "User",
        "idempotencyKey": idempotency_key,
    }
    if offer.seller_id is not None:
        payload["expectedSellerId"] = offer.seller_id
    if offer.kind == RESALE:
        payload["collectibleItemInstanceId"] = offer.instance_id
    return payload


async def purchase(s: RobloxSession, offer: Offer, buyer_id: int, idempotency_key: str) -> PurchaseResult:
    endpoint = "purchase-resale" if offer.kind == RESALE else "purchase-item"
    assert offer.kind in (STOCK, RESALE)
    r = await s.request(
        "POST",
        f"{APIS}/marketplace-sales/v1/item/{offer.collectible_item_id}/{endpoint}",
        json=purchase_payload(offer, buyer_id, idempotency_key),
    )
    try:
        body = r.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {"data": body}
    if r.status_code == 200 and body.get("purchased") is True:
        return PurchaseResult(True, 200, None, body)
    error = (
        body.get("errorMessage")
        or body.get("purchaseResult")
        or (body.get("errors") or [{}])[0].get("message")
        or r.text[:200]
        or f"HTTP {r.status_code}"
    )
    return PurchaseResult(False, r.status_code, str(error), body)
