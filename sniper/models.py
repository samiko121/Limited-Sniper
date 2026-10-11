from __future__ import annotations

from dataclasses import dataclass, field

STOCK = "stock"    # bought from the creator (original stock)
RESALE = "resale"  # bought from another player's listing


@dataclass
class ItemMeta:
    """What we have learned about a watched asset from Roblox responses."""
    name: str | None = None
    collectible_item_id: str | None = None
    collectible_product_id: str | None = None
    creator_id: int | None = None
    creator_type: str = "User"
    quantity_limit_per_user: int | None = None
    not_collectible: bool = False


@dataclass
class Offer:
    kind: str
    asset_id: int
    name: str
    collectible_item_id: str
    price: int
    collectible_product_id: str | None = None
    instance_id: str | None = None
    seller_id: int | None = None
    seller_type: str = "User"
    serial: int | None = None
    quantity_limit_per_user: int | None = None
    source: str = ""

    @property
    def complete(self) -> bool:
        if not self.collectible_product_id:
            return False
        if self.kind == RESALE:
            return bool(self.instance_id) and self.seller_id is not None
        return True

    @property
    def key(self) -> tuple:
        return (self.collectible_item_id, self.kind, self.instance_id or self.collectible_product_id, self.price)


@dataclass
class PurchaseResult:
    success: bool
    status: int = 0
    error: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def insufficient_funds(self) -> bool:
        return bool(self.error and "InsufficientBalance" in self.error)

    @property
    def rate_limited(self) -> bool:
        return self.status == 429
