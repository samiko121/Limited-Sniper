import asyncio

import pytest

from sniper import roblox
from sniper.models import RESALE, STOCK, Offer

from .conftest import COOKIE
from .fake_roblox import FakeRoblox


def run(coro):
    return asyncio.run(coro)


def test_cookie_sent_only_to_roblox_and_csrf_retry():
    fake = FakeRoblox()

    async def go():
        s = roblox.RobloxSession(COOKIE, transport=fake.transport())
        uid, name = await roblox.get_authenticated_user(s)
        assert (uid, name) == (42, "tester")
        rows = await roblox.catalog_details(s, [1])  # POST: first 403, then retried with token
        await s.aclose()
        return rows

    run(go())
    posts = [r for r in fake.requests if r.method == "POST"]
    assert len(posts) == 2
    assert "x-csrf-token" not in posts[0].headers
    assert posts[1].headers["x-csrf-token"] == "token-1"
    assert all(".ROBLOSECURITY" in r.headers.get("cookie", "") for r in fake.requests)


def test_unauthorized_raises():
    fake = FakeRoblox()

    async def go():
        s = roblox.RobloxSession(None, transport=fake.transport())
        try:
            await roblox.get_authenticated_user(s)
        finally:
            await s.aclose()

    with pytest.raises(roblox.Unauthorized):
        run(go())


def test_purchase_payloads():
    fake = FakeRoblox()
    stock = Offer(STOCK, 1, "x", "cid", 10, collectible_product_id="pid", seller_id=7, seller_type="Group")
    resale = Offer(RESALE, 1, "x", "cid", 20, collectible_product_id="rp", instance_id="inst", seller_id=9)

    async def go():
        s = roblox.RobloxSession(COOKIE, transport=fake.transport())
        r1 = await roblox.purchase(s, stock, 42, "key-1")
        r2 = await roblox.purchase(s, resale, 42, "key-2")
        await s.aclose()
        return r1, r2

    r1, r2 = run(go())
    assert r1.success and r2.success
    p1, p2 = fake.purchases
    assert p1["path"].endswith("/item/cid/purchase-item")
    assert p1["expectedPrice"] == 10 and p1["expectedSellerId"] == 7 and p1["expectedSellerType"] == "Group"
    assert p1["collectibleProductId"] == "pid" and "collectibleItemInstanceId" not in p1
    assert p1["expectedPurchaserId"] == 42 and p1["idempotencyKey"] == "key-1"
    assert p2["path"].endswith("/purchase-resale")
    assert p2["collectibleItemInstanceId"] == "inst" and p2["expectedSellerId"] == 9


def test_purchase_failure_message():
    fake = FakeRoblox()
    fake.purchase_body = {"purchased": False, "errorMessage": "QuantityExhausted"}
    offer = Offer(STOCK, 1, "x", "cid", 10, collectible_product_id="pid", seller_id=7)

    async def go():
        s = roblox.RobloxSession(COOKIE, transport=fake.transport())
        r = await roblox.purchase(s, offer, 42, "k")
        await s.aclose()
        return r

    r = run(go())
    assert not r.success and r.error == "QuantityExhausted"
