import asyncio
import json

import pytest

from sniper import roblox
from sniper.config import AUTO, CLASSIC, UGC, ConfigError, ConfigStore, WatchItem
from sniper.buyer import guard_reason
from sniper.engine import Engine
from sniper.history import History
from sniper.lanes import Lane, LanePool
from sniper.models import RESALE, Offer
from sniper.notify import Notifier
from sniper.rolimons import RolimonsValues, build_auto_items, deal_percent, max_price_for, parse_itemdetails

from .conftest import COOKIE
from .fake_roblox import FakeRoblox

# real rows from https://www.rolimons.com/itemapi/itemdetails (Oct 2026) + made-up edge cases
ITEMDETAILS = {
    "success": True,
    "item_count": 5,
    "items": {
        "1028606": ["Red Baseball Cap", "", 1315, -1, 1315, -1, -1, -1, -1, -1],           # unvalued
        "1029025": ["The Classic ROBLOX Fedora", "CF", 406464, 450000, 450000, 4, 2, -1, -1, -1],
        "1111": ["Projected Hat", "PH", 90000, 20000, 20000, 1, 2, 1, -1, -1],             # projected
        "2222": ["Cheap Valued", "", 900, 1200, 1200, 1, 2, -1, -1, -1],                  # 26% below 1200 = 888 < 1000
        "3333": ["Huge Valued", "", 5000000, 5000000, 5000000, 4, 2, -1, -1, -1],         # capped at 1,000,000
    },
}


def test_parse_itemdetails():
    items = parse_itemdetails(ITEMDETAILS)
    assert items[1028606].value is None and items[1028606].rap == 1315
    assert items[1029025].value == 450000 and not items[1029025].projected
    assert items[1111].projected


def test_deal_math():
    assert max_price_for(450000, 26) == 333000
    assert deal_percent(450000, 333000) == pytest.approx(26.0)
    assert max_price_for(1000, 26) == 740
    assert max_price_for(999, 0) == 999


def test_default_auto_items(config_file):
    s = config_file().settings  # defaults: valued only, skip projecteds, 26%, 1000..1000000
    auto = build_auto_items(parse_itemdetails(ITEMDETAILS), s)
    assert set(auto) == {1029025, 3333}
    fedora = auto[1029025]
    assert fedora.category == AUTO and fedora.max_price == 333000 and fedora.ref_value == 450000
    assert fedora.min_price == 1000
    assert auto[3333].max_price == 1000000  # Ignore_prices_above caps it


def test_auto_options(config_file):
    store = config_file()
    raw = store.raw["Classic"]["Auto_deals"]
    raw.update(Only_valued_items=False, Skip_known_projecteds=False, Ignore_prices_below=0, Deal_calculation="Value/(RAP)")
    store.path.write_text(json.dumps(store.raw))
    s = store.load()
    assert s.deal_calculation == "value_or_rap"
    auto = build_auto_items(parse_itemdetails(ITEMDETAILS), s)
    assert set(auto) == {1028606, 1029025, 1111, 2222, 3333}
    assert auto[1028606].ref_value == 1315  # unvalued -> RAP
    store.raw["Classic"]["Auto_deals"]["Deal_calculation"] = "rap"
    store.path.write_text(json.dumps(store.raw))
    assert build_auto_items(parse_itemdetails(ITEMDETAILS), store.load())[1029025].ref_value == 406464


def test_bad_auto_settings(config_file):
    with pytest.raises(ConfigError):
        config_file(Classic={"Items": {}, "Auto_deals": {"Min_deal_percent": 150}})
    with pytest.raises(ConfigError):
        config_file(Classic={"Items": {}, "Auto_deals": {"Deal_calculation": "vibes"}})


def test_old_items_migrate_to_ugc_and_split_saving(config_file):
    store = config_file(Items={"15181542808": 1})
    assert store.settings.items[15181542808].category == UGC
    store.set_items({1: WatchItem(1, 5, None, UGC), 2: WatchItem(2, 9, None, CLASSIC)})
    raw = json.loads(store.path.read_text())
    assert raw["UGC"]["Items"] == {"1": 5} and raw["Classic"]["Items"] == {"2": 9}
    assert "Items" not in raw


def test_same_id_in_both_lists_rejected(config_file):
    with pytest.raises(ConfigError):
        config_file(UGC={"Items": {"5": 1}}, Classic={"Items": {"5": 1}, "Auto_deals": {}})


def test_min_price_guard(config_file):
    s = config_file().settings
    item = WatchItem(1, 333000, None, AUTO, min_price=1000, ref_value=450000)
    cheap = Offer(RESALE, 1, "x", "c", 500, collectible_product_id="p")
    assert "Ignore_prices_below" in guard_reason(cheap, item, s, robux=10**9, spent_today=0, pending=0, owned=None)
    ok = Offer(RESALE, 1, "x", "c", 300000, collectible_product_id="p")
    assert guard_reason(ok, item, s, robux=10**9, spent_today=0, pending=0, owned=None) is None


def test_engine_snipes_classic_auto_deal(config_file, tmp_path):
    """Red Baseball Cap listed at 1400 with a (pretend) value of 2000 -> 30% deal."""
    store, fake = config_file(Classic={"Items": {}, "Auto_deals": {"Only_valued_items": True}}), FakeRoblox()
    roli = RolimonsValues(tmp_path / "roli.json")
    roli.items = parse_itemdetails({"success": True, "items": {"1028606": ["Red Baseball Cap", "", 1315, 2000, 2000, 1, 2, -1, -1, -1]}})

    async def go():
        buy = roblox.RobloxSession(COOKIE, transport=fake.transport())
        lanes = LanePool([Lane(roblox.RobloxSession(None, transport=fake.transport()), rates=50)])
        e = Engine(store, buy, lanes, History(tmp_path / "h.db"), Notifier(None, False), roli)
        assert e.counts() == {UGC: 0, CLASSIC: 0, AUTO: 1}
        assert e.auto_items[1028606].max_price == 1480
        await e.login()
        e.start()
        await asyncio.sleep(1.5)
        await e.stop()
        await lanes.aclose(); await buy.aclose(); await roli.aclose()
        return e

    e = asyncio.run(go())
    assert len(fake.purchases) == 1 and fake.purchases[0]["expectedPrice"] == 1400
    assert any("30% below value 2,000" in ev[2] for ev in e.stats.events)
