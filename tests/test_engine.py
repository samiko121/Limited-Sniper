import asyncio

from sniper import roblox
from sniper.commands import Commands, CommandError
from sniper.config import WatchItem
from sniper.engine import Engine
from sniper.history import History
from sniper.lanes import Lane, LanePool
from sniper.notify import Notifier
from sniper.console import run_command

from .conftest import COOKIE
from .fake_roblox import FakeRoblox


def build(store, fake, tmp_path):
    buy = roblox.RobloxSession(COOKIE, transport=fake.transport(), name="buyer")
    lanes = LanePool([Lane(roblox.RobloxSession(None, transport=fake.transport(), name="direct"), rates=50)])
    return Engine(store, buy, lanes, History(tmp_path / "h.db"), Notifier(None, False))


async def run_engine(engine, seconds):
    await engine.login()
    engine.start()
    await asyncio.sleep(seconds)
    await engine.stop()
    await engine.lanes.aclose()
    await engine.buy_session.aclose()


def test_end_to_end_snipes_resale_under_max(config_file, tmp_path):
    store, fake = config_file(Items={"1028606": 1500, "999": 5}), FakeRoblox()
    engine = build(store, fake, tmp_path)
    asyncio.run(run_engine(engine, 1.5))
    assert engine.stats.username == "tester"
    assert len(fake.purchases) == 1, fake.purchases
    p = fake.purchases[0]
    assert p["path"].endswith("/purchase-resale") and p["expectedPrice"] == 1400
    assert engine.stats.buys == 1 and engine.stats.checks > 0
    # 999 does not exist in the catalog -> flagged instead of retried forever
    assert engine.watchlist.meta[999].not_collectible
    # every watcher endpoint was used
    paths = {r.url.path for r in fake.requests}
    assert "/marketplace-items/v1/items/details" in paths
    assert "/v1/catalog/items/details" in paths
    assert any(p.startswith("/v2/assets/") for p in paths)


def test_end_to_end_ignores_items_above_max(config_file, tmp_path):
    store, fake = config_file(Items={"1028606": 1000}), FakeRoblox()
    engine = build(store, fake, tmp_path)
    asyncio.run(run_engine(engine, 1.0))
    assert fake.purchases == []
    assert not any(r.url.path.endswith("/resellers") for r in fake.requests)


def test_pause_stops_buying(config_file, tmp_path):
    store, fake = config_file(Items={"1028606": 1500}), FakeRoblox()
    engine = build(store, fake, tmp_path)
    engine.paused = True
    asyncio.run(run_engine(engine, 0.8))
    assert fake.purchases == []


def test_commands_update_config_and_watchlist(config_file, tmp_path):
    store, fake = config_file(Items={"1": 5}), FakeRoblox()
    engine = build(store, fake, tmp_path)
    c = Commands(engine)
    c.add("https://www.roblox.com/catalog/1028606/Red-Baseball-Cap", 1500, 2)
    assert store.settings.items[1028606] == WatchItem(1028606, 1500, 2)
    assert engine.watchlist.get(1028606).max_price == 1500
    c.price_all(9)
    assert {i.max_price for i in store.settings.items.values()} == {9}
    assert store.settings.items[1028606].buy_limit == 2  # kept
    c.mass_add(3, ["10", "11", "bad"])
    assert store.settings.items[10].max_price == 3
    c.remove("10")
    assert 10 not in engine.watchlist.items
    try:
        c.remove("10")
        raise AssertionError("expected CommandError")
    except CommandError:
        pass
    c.focus(1028606, 50)
    assert engine.watchlist.active_ids() == [1028606]
    c.unfocus()
    c.dry_run("on")
    assert store.settings.dry_run
    # the reload from disk sees the same data
    store.load()
    assert 11 in store.settings.items
    out = run_command(c, "add 12345 7")
    assert out == ["Added UGC item 12345 with max price 7 R$"]
    assert run_command(c, "price nope 5")[0].startswith("error:")
    asyncio.run(engine.lanes.aclose())
    asyncio.run(engine.buy_session.aclose())


def test_stock_respects_buy_limit_under_concurrency(config_file, tmp_path):
    from .conftest import RED_CAP_ECONOMY
    store, fake = config_file(Items={"1028606": {"max_price": 10, "buy_limit": 2}}), FakeRoblox()
    fake.economy = dict(RED_CAP_ECONOMY, IsForSale=True, Remaining=50, PriceInRobux=5)
    fake.marketplace = dict(fake.marketplace, unitsAvailableForConsumption=50, price=5, hasResellers=False)
    engine = build(store, fake, tmp_path)
    asyncio.run(run_engine(engine, 2.0))
    assert [p["path"].rsplit("/", 1)[-1] for p in fake.purchases] == ["purchase-item", "purchase-item"]
    assert all(p["expectedPrice"] == 5 and p["expectedSellerId"] == 1 for p in fake.purchases)


def test_discord_bot_registers_commands(config_file, tmp_path):
    from sniper.discord_bot import SniperBot

    store, fake = config_file(Discord={"Owner_ids": [1]}), FakeRoblox()

    async def go():
        engine = build(store, fake, tmp_path)
        bot = SniperBot(engine, Commands(engine))
        names = sorted(c.name for c in bot.tree.get_commands())
        await bot.close()
        await engine.lanes.aclose()
        await engine.buy_session.aclose()
        return names

    names = asyncio.run(go())
    assert len(names) >= 20
    for n in ("add_item", "mass_add_ids", "remove_item", "change_max_price", "focus", "stats", "help", "restart"):
        assert n in names


def test_dashboard_renders(config_file, tmp_path):
    from rich.console import Console
    from sniper.console import ConsoleUI

    store, fake = config_file(Items={"1028606": 1500}), FakeRoblox()
    engine = build(store, fake, tmp_path)
    engine.stats.event("BOUGHT Red Baseball Cap (resale) for 1400 R$ in 80 ms")
    ui = ConsoleUI(engine, Commands(engine), dashboard=True)
    console = Console(record=True, width=120)
    console.print(ui.render())
    text = console.export_text()
    assert "Limited Sniper" in text and "BOUGHT Red Baseball Cap" in text
    asyncio.run(engine.lanes.aclose())
    asyncio.run(engine.buy_session.aclose())
