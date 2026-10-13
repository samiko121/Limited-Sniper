import copy

from sniper.models import RESALE, STOCK
from sniper.parsers import (
    meta_from_catalog,
    offer_from_reseller,
    offers_from_catalog,
    offers_from_economy,
    offers_from_marketplace,
)

from .conftest import NEW_UGC_CATALOG, RED_CAP_ECONOMY, RED_CAP_MARKETPLACE, RESELLER_ROW


def test_economy_classic_limited_is_resale_only():
    offers = offers_from_economy(RED_CAP_ECONOMY)
    assert [o.kind for o in offers] == [RESALE]
    o = offers[0]
    assert o.price == 1400 and o.asset_id == 1028606
    assert o.collectible_product_id == "2b06ee00-6e5c-48e0-9634-9a8c4ebac43f"
    assert o.instance_id == "1b61f7cb-1079-4ffb-8880-5eba8129a62f"
    assert not o.complete  # seller still unknown -> confirmed via /resellers


def test_economy_stock_offer():
    d = copy.deepcopy(RED_CAP_ECONOMY)
    d.update(IsForSale=True, Remaining=12, PriceInRobux=50)
    stock = [o for o in offers_from_economy(d) if o.kind == STOCK][0]
    assert stock.price == 50
    assert stock.collectible_product_id == "7feb9642-a961-4a94-bd35-3c865d12dac1"
    assert stock.seller_id == 1 and stock.seller_type == "User"
    assert stock.complete


def test_economy_non_collectible_has_no_offers():
    d = copy.deepcopy(RED_CAP_ECONOMY)
    d["CollectibleItemId"] = None
    assert offers_from_economy(d) == []


def test_marketplace_offers():
    offers = offers_from_marketplace(RED_CAP_MARKETPLACE)
    assert [o.kind for o in offers] == [RESALE]
    d = dict(RED_CAP_MARKETPLACE, unitsAvailableForConsumption=3, price=10)
    stock = [o for o in offers_from_marketplace(d) if o.kind == STOCK][0]
    assert stock.price == 10 and stock.seller_id == 1 and stock.complete


def test_marketplace_experience_only_stock_is_skipped():
    d = dict(RED_CAP_MARKETPLACE, unitsAvailableForConsumption=3, saleLocationType="ExperiencesDevApiOnly", hasResellers=False)
    assert offers_from_marketplace(d) == []


def test_free_item_price_zero_is_an_offer():
    offers = offers_from_catalog(NEW_UGC_CATALOG, product_id=None)
    assert len(offers) == 1 and offers[0].kind == STOCK and offers[0].price == 0
    assert offers[0].quantity_limit_per_user == 2
    assert not offers[0].complete  # needs product id from economy
    assert offers_from_catalog(NEW_UGC_CATALOG, product_id="p")[0].complete


def test_catalog_meta():
    m = meta_from_catalog(NEW_UGC_CATALOG)
    assert m.collectible_item_id == "94784696-2739-4266-92c9-49c6367f161d"
    assert not m.not_collectible
    assert meta_from_catalog({"id": 5, "itemRestrictions": []}).not_collectible


def test_offer_from_reseller_fills_seller():
    base = offers_from_economy(RED_CAP_ECONOMY)[0]
    o = offer_from_reseller(RESELLER_ROW, base)
    assert o.seller_id == 4637356944 and o.complete and o.price == 1400


def test_economy_in_game_only_stock_is_skipped():
    d = copy.deepcopy(RED_CAP_ECONOMY)
    d.update(IsForSale=True, Remaining=5000, PriceInRobux=0, SaleLocation={"SaleLocationType": 6, "UniverseIds": [7554119808]})
    assert [o.kind for o in offers_from_economy(d)] == [RESALE]  # resale is still fine
    d["SaleLocation"] = {"SaleLocationType": 5, "UniverseIds": []}
    assert STOCK in [o.kind for o in offers_from_economy(d)]
