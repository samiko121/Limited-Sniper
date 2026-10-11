"""Turn raw Roblox responses into Offers.

Each endpoint has a different shape. Since 2024 Roblox has moved classic
limiteds onto the collectibles system too, so every buyable limited has a
collectibleItemId and is bought through marketplace-sales (the old
economy/v1/purchases path the previous sniper used for classic limiteds is gone).
"""

from __future__ import annotations

from .models import RESALE, STOCK, ItemMeta, Offer


def _shop_purchasable(sale_location: object) -> bool:
    # String values look like "ShopOnly", "ShopAndAllExperiences",
    # "ExperiencesDevApiOnly", ... Only the ones that include the shop can be
    # bought through the website API. Unknown / numeric values are allowed.
    if isinstance(sale_location, str) and sale_location:
        return "Shop" in sale_location
    return True


def _int_or_none(v: object) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def meta_from_economy(d: dict) -> ItemMeta:
    creator = d.get("Creator") or {}
    cd = d.get("CollectiblesItemDetails") or {}
    return ItemMeta(
        name=d.get("Name"),
        collectible_item_id=d.get("CollectibleItemId"),
        collectible_product_id=d.get("CollectibleProductId"),
        creator_id=_int_or_none(creator.get("CreatorTargetId") or creator.get("Id")),
        creator_type=creator.get("CreatorType") or "User",
        quantity_limit_per_user=_int_or_none(cd.get("CollectibleQuantityLimitPerUser")) or None,
        not_collectible=not d.get("CollectibleItemId"),
    )


def offers_from_economy(d: dict) -> list[Offer]:
    meta = meta_from_economy(d)
    cid = meta.collectible_item_id
    if not cid:
        return []
    asset_id = int(d.get("AssetId") or d.get("TargetId"))
    name = meta.name or str(asset_id)
    offers: list[Offer] = []

    price = _int_or_none(d.get("PriceInRobux"))
    remaining = _int_or_none(d.get("Remaining"))
    sale_location = (d.get("SaleLocation") or {}).get("SaleLocationType")
    if (
        d.get("IsForSale")
        and price is not None
        and (remaining is None or remaining > 0)
        and meta.collectible_product_id
        and _shop_purchasable(sale_location)
    ):
        offers.append(Offer(
            STOCK, asset_id, name, cid, price,
            collectible_product_id=meta.collectible_product_id,
            seller_id=meta.creator_id, seller_type=meta.creator_type,
            quantity_limit_per_user=meta.quantity_limit_per_user, source="economy",
        ))

    cd = d.get("CollectiblesItemDetails") or {}
    resale = _int_or_none(cd.get("CollectibleLowestResalePrice"))
    if resale is not None and resale > 0 and cd.get("CollectibleLowestAvailableResaleProductId"):
        offers.append(Offer(
            RESALE, asset_id, name, cid, resale,
            collectible_product_id=cd.get("CollectibleLowestAvailableResaleProductId"),
            instance_id=cd.get("CollectibleLowestAvailableResaleItemInstanceId"),
            quantity_limit_per_user=meta.quantity_limit_per_user, source="economy",
        ))
    return offers


def meta_from_marketplace(d: dict) -> ItemMeta:
    return ItemMeta(
        name=d.get("name"),
        collectible_item_id=d.get("collectibleItemId"),
        collectible_product_id=d.get("collectibleProductId"),
        creator_id=_int_or_none(d.get("creatorId")),
        creator_type=d.get("creatorType") or "User",
        quantity_limit_per_user=_int_or_none(d.get("quantityLimitPerUser")) or None,
    )


def offers_from_marketplace(d: dict) -> list[Offer]:
    meta = meta_from_marketplace(d)
    cid = meta.collectible_item_id
    asset_id = _int_or_none(d.get("itemTargetId"))
    if not cid or asset_id is None:
        return []
    name = meta.name or str(asset_id)
    offers: list[Offer] = []

    price = _int_or_none(d.get("price"))
    units = _int_or_none(d.get("unitsAvailableForConsumption")) or 0
    if price is not None and units > 0 and meta.collectible_product_id and _shop_purchasable(d.get("saleLocationType")):
        offers.append(Offer(
            STOCK, asset_id, name, cid, price,
            collectible_product_id=meta.collectible_product_id,
            seller_id=meta.creator_id, seller_type=meta.creator_type,
            quantity_limit_per_user=meta.quantity_limit_per_user, source="marketplace",
        ))

    resale = _int_or_none(d.get("lowestResalePrice"))
    if d.get("hasResellers") and resale is not None and resale > 0 and d.get("lowestAvailableResaleProductId"):
        offers.append(Offer(
            RESALE, asset_id, name, cid, resale,
            collectible_product_id=d.get("lowestAvailableResaleProductId"),
            instance_id=d.get("lowestAvailableResaleItemInstanceId"),
            quantity_limit_per_user=meta.quantity_limit_per_user, source="marketplace",
        ))
    return offers


def meta_from_catalog(d: dict) -> ItemMeta:
    restrictions = d.get("itemRestrictions") or []
    return ItemMeta(
        name=d.get("name"),
        collectible_item_id=d.get("collectibleItemId"),
        creator_id=_int_or_none(d.get("creatorTargetId")),
        creator_type=d.get("creatorType") or "User",
        quantity_limit_per_user=_int_or_none(d.get("quantityLimitPerUser")) or None,
        not_collectible=not d.get("collectibleItemId") and not any(
            r in restrictions for r in ("Collectible", "Limited", "LimitedUnique")
        ),
    )


def offers_from_catalog(d: dict, product_id: str | None) -> list[Offer]:
    """The catalog batch endpoint covers 120 items per request but has no
    product / listing ids. Stock offers are complete only if the product id is
    already known; resale offers are always confirmed with the resellers call."""
    meta = meta_from_catalog(d)
    cid = meta.collectible_item_id
    asset_id = _int_or_none(d.get("id"))
    if not cid or asset_id is None:
        return []
    name = meta.name or str(asset_id)
    offers: list[Offer] = []

    price = _int_or_none(d.get("price"))
    units = _int_or_none(d.get("unitsAvailableForConsumption")) or 0
    off_sale = d.get("isOffSale") or d.get("priceStatus") == "Off Sale"
    if price is not None and units > 0 and not off_sale and _shop_purchasable(d.get("saleLocationType")):
        offers.append(Offer(
            STOCK, asset_id, name, cid, price,
            collectible_product_id=product_id,
            seller_id=meta.creator_id, seller_type=meta.creator_type,
            quantity_limit_per_user=meta.quantity_limit_per_user, source="catalog",
        ))

    resale = _int_or_none(d.get("lowestResalePrice"))
    if d.get("hasResellers") and resale is not None and resale > 0:
        offers.append(Offer(
            RESALE, asset_id, name, cid, resale,
            quantity_limit_per_user=meta.quantity_limit_per_user, source="catalog",
        ))
    return offers


def offer_from_reseller(row: dict, base: Offer) -> Offer | None:
    """Build a complete resale offer from a /resellers row."""
    price = _int_or_none(row.get("price"))
    if price is None or not row.get("collectibleProductId") or not row.get("collectibleItemInstanceId"):
        return None
    seller = row.get("seller") or {}
    return Offer(
        RESALE, base.asset_id, base.name, base.collectible_item_id, price,
        collectible_product_id=row["collectibleProductId"],
        instance_id=row["collectibleItemInstanceId"],
        seller_id=_int_or_none(seller.get("sellerId")),
        seller_type=seller.get("sellerType") or "User",
        serial=_int_or_none(row.get("serialNumber")),
        quantity_limit_per_user=base.quantity_limit_per_user,
        source=base.source,
    )
