import asyncio
import json
import time

from sniper import roblox
from sniper.buyer import Buyer, effective_limit, guard_reason
from sniper.config import WatchItem
from sniper.history import History, Purchase
from sniper.models import RESALE, STOCK, Offer
from sniper.notify import Notifier
from sniper.parsers import offers_from_economy
from sniper.state import Stats, Watchlist

from .conftest import COOKIE, RED_CAP_ECONOMY
from .fake_roblox import FakeRoblox


def make_buyer(store, fake, tmp_path):
    session = roblox.RobloxSession(COOKIE, transport=fake.transport())
    stats = Stats()
    stats.robux = fake.robux
    history = History(tmp_path / "h.db")
    buyer = Buyer(session, lambda: store.settings, Watchlist(), stats, history, Notifier(None, False))
    buyer.user_id = 42
    return buyer, history


def run_buy(store, fake, tmp_path, offers, item, repeat=1):
    async def go():
        buyer, history = make_buyer(store, fake, tmp_path)
        for _ in range(repeat):
            for o in offers:
                buyer.consider(o, item)
        await asyncio.sleep(0)
        await buyer.drain()
        await asyncio.sleep(0.05)  # let _after_buy finish
        await buyer.drain()
        rows = history.recent(20)
        await buyer.session.aclose()
        return buyer, rows

    return asyncio.run(go())


def red_cap_resale():
    return offers_from_economy(RED_CAP_ECONOMY)[0]


def test_resale_confirmed_and_bought(config_file, tmp_path):
    store, fake = config_file(), FakeRoblox()
    buyer, rows = run_buy(store, fake, tmp_path, [red_cap_resale()], WatchItem(1028606, 1500))
    assert len(fake.purchases) == 1
    p = fake.purchases[0]
    assert p["path"].endswith("/purchase-resale")
    assert p["expectedSellerId"] == 4637356944 and p["expectedPrice"] == 1400
    assert buyer.stats.buys == 1 and rows[0].status == "bought"


def test_price_rechecked_on_live_listing(config_file, tmp_path):
    store, fake = config_file(), FakeRoblox()
    fake.reseller = dict(fake.reseller, price=2000)  # listing changed since detection
    run_buy(store, fake, tmp_path, [red_cap_resale()], WatchItem(1028606, 1500))
    assert fake.purchases == []


def test_dry_run_never_buys(config_file, tmp_path):
    store, fake = config_file(Safety={"Dry_run": True}), FakeRoblox()
    buyer, rows = run_buy(store, fake, tmp_path, [red_cap_resale()], WatchItem(1028606, 1500))
    assert fake.purchases == []
    assert rows[0].status == "dry_run"


def test_buy_limit(config_file, tmp_path):
    store, fake = config_file(), FakeRoblox()
    fake.owned = 2
    run_buy(store, fake, tmp_path, [red_cap_resale()], WatchItem(1028606, 1500, buy_limit=2))
    assert fake.purchases == []


def test_global_buy_limit(config_file, tmp_path):
    store, fake = config_file(Global_buy_limit={"Enabled": True, "Limit": 1}), FakeRoblox()
    fake.owned = 1
    run_buy(store, fake, tmp_path, [red_cap_resale()], WatchItem(1028606, 1500))
    assert fake.purchases == []


def test_same_listing_only_attempted_once(config_file, tmp_path):
    store, fake = config_file(), FakeRoblox()
    run_buy(store, fake, tmp_path, [red_cap_resale()], WatchItem(1028606, 1500), repeat=5)
    assert len(fake.purchases) == 1


def test_network_retry_reuses_idempotency_key(config_file, tmp_path):
    store, fake = config_file(), FakeRoblox()
    fake.fail_network_once = True
    offer = Offer(STOCK, 1, "x", "cid", 10, collectible_product_id="pid", seller_id=1)
    run_buy(store, fake, tmp_path, [offer], WatchItem(1, 10))
    attempts = [r for r in fake.requests if r.url.path.endswith("/purchase-item")]
    keys = {json.loads(r.content)["idempotencyKey"] for r in attempts}
    assert len(attempts) >= 2 and len(keys) == 1  # (+1 for the first CSRF handshake)
    assert len(fake.purchases) == 1


def test_daily_limit_counts_history(config_file, tmp_path):
    store, fake = config_file(Safety={"Daily_spend_limit": 1500}), FakeRoblox()
    History(tmp_path / "h.db").add(Purchase(time.time(), 5, "earlier", 200, STOCK, "bought", None, None))
    run_buy(store, fake, tmp_path, [red_cap_resale()], WatchItem(1028606, 1500))
    assert fake.purchases == []  # 200 + 1400 > 1500


def test_guard_reason_rules(config_file):
    s = config_file(Safety={"Max_price_per_purchase": 100, "Keep_robux_reserve": 50}).settings
    o = Offer(STOCK, 1, "x", "c", 60, collectible_product_id="p")
    item = WatchItem(1, 80)
    assert guard_reason(o, item, s, robux=200, spent_today=0, pending=0, owned=None) is None
    assert "reserve" in guard_reason(o, item, s, robux=100, spent_today=0, pending=0, owned=None)
    assert "pending" not in (guard_reason(o, item, s, robux=200, spent_today=0, pending=100, owned=None) or "pending")
    assert "Max_price_per_purchase" in guard_reason(Offer(STOCK, 1, "x", "c", 150), WatchItem(1, 200), s, robux=999, spent_today=0, pending=0, owned=None)
    assert "> max" in guard_reason(Offer(STOCK, 1, "x", "c", 90), item, s, robux=999, spent_today=0, pending=0, owned=None)
    free = Offer(STOCK, 1, "x", "c", 0, collectible_product_id="p")
    assert guard_reason(free, item, s, robux=0, spent_today=0, pending=0, owned=None) is None


def test_effective_limit(config_file):
    s = config_file(Global_buy_limit={"Enabled": True, "Limit": 3}).settings
    assert effective_limit(WatchItem(1, 1), s, None) == 3
    assert effective_limit(WatchItem(1, 1, 5), s, None) == 5
    assert effective_limit(WatchItem(1, 1, 5), s, 2) == 2
    s2 = config_file().settings
    assert effective_limit(WatchItem(1, 1), s2, None) is None


def test_dry_run_reports_a_listing_once(config_file, tmp_path):
    store, fake = config_file(Safety={"Dry_run": True}), FakeRoblox()

    async def go():
        buyer, history = make_buyer(store, fake, tmp_path)
        for _ in range(2):
            buyer.consider(red_cap_resale(), WatchItem(1028606, 1500))
            await asyncio.sleep(0)
            await buyer.drain()
        rows = history.recent(20)
        left = max(buyer.cooldown.values()) - time.monotonic()
        await buyer.session.aclose()
        return rows, left

    rows, left = asyncio.run(go())
    assert [r.status for r in rows] == ["dry_run"]
    assert left > 300  # not re-reported every minute while it stays listed
