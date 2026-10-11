import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sniper.config import DEFAULTS, ConfigStore  # noqa: E402

# Shapes captured from the live Roblox APIs (Oct 2026), trimmed.
RED_CAP_ECONOMY = {
    "TargetId": 1028606, "ProductType": "Collectible Item", "AssetId": 1028606, "ProductId": 2744785375,
    "Name": "Red Baseball Cap",
    "Creator": {"Id": 1, "Name": "Roblox", "CreatorType": "User", "CreatorTargetId": 1, "HasVerifiedBadge": True},
    "PriceInRobux": 7, "IsForSale": False, "IsLimited": True, "IsLimitedUnique": False, "Remaining": 0,
    "SaleLocation": {"SaleLocationType": 1, "UniverseIds": []},
    "CollectibleItemId": "f9111724-5901-45f6-9b30-4ce57bd919db",
    "CollectibleProductId": "7feb9642-a961-4a94-bd35-3c865d12dac1",
    "CollectiblesItemDetails": {
        "CollectibleLowestResalePrice": 1400,
        "CollectibleLowestAvailableResaleProductId": "2b06ee00-6e5c-48e0-9634-9a8c4ebac43f",
        "CollectibleLowestAvailableResaleItemInstanceId": "1b61f7cb-1079-4ffb-8880-5eba8129a62f",
        "CollectibleQuantityLimitPerUser": None, "IsForSale": False, "TotalQuantity": 118959, "IsLimited": True,
    },
}

RED_CAP_MARKETPLACE = {
    "collectibleItemId": "f9111724-5901-45f6-9b30-4ce57bd919db", "name": "Red Baseball Cap",
    "collectibleProductId": "7feb9642-a961-4a94-bd35-3c865d12dac1", "itemType": 1, "itemRestrictions": None,
    "creatorType": "User", "itemTargetId": 1028606, "creatorId": 1, "price": 7, "lowestPrice": 1400,
    "hasResellers": True, "unitsAvailableForConsumption": 0, "saleLocationType": "ShopOnly",
    "lowestResalePrice": 1400, "quantityLimitPerUser": 0,
    "lowestAvailableResaleProductId": "2b06ee00-6e5c-48e0-9634-9a8c4ebac43f",
    "lowestAvailableResaleItemInstanceId": "1b61f7cb-1079-4ffb-8880-5eba8129a62f",
}

NEW_UGC_CATALOG = {
    "id": 126964119183805, "itemType": "Asset", "assetType": 43, "name": "Lya Halloween",
    "productId": 6167935088068222, "itemRestrictions": ["Collectible"], "creatorType": "User",
    "creatorTargetId": 9724411196, "price": 0, "lowestPrice": 0, "lowestResalePrice": 0, "priceStatus": "Free",
    "unitsAvailableForConsumption": 1, "collectibleItemId": "94784696-2739-4266-92c9-49c6367f161d",
    "totalQuantity": 2, "saleLocationType": "ShopAndAllExperiences", "hasResellers": False, "quantityLimitPerUser": 2,
}

RESELLER_ROW = {
    "collectibleProductId": "2b06ee00-6e5c-48e0-9634-9a8c4ebac43f",
    "collectibleItemInstanceId": "1b61f7cb-1079-4ffb-8880-5eba8129a62f",
    "seller": {"hasVerifiedBadge": False, "sellerId": 4637356944, "sellerType": "User", "name": "SomeSeller"},
    "price": 1400, "serialNumber": None, "errorMessage": None,
}

COOKIE = "_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-in-as-you-and-to-steal-your-ROBUX-and-items.|_TESTCOOKIE123"


@pytest.fixture
def config_file(tmp_path):
    def make(**overrides):
        raw = json.loads(json.dumps(DEFAULTS))
        raw["Cookies"]["Buy_cookie"] = COOKIE
        for section, values in overrides.items():
            if isinstance(values, dict) and isinstance(raw.get(section), dict) and section != "Items":
                raw[section].update(values)
            else:
                raw[section] = values
        p = tmp_path / "config.json"
        p.write_text(json.dumps(raw), encoding="utf-8")
        store = ConfigStore(p)
        store.load()
        return store
    return make
