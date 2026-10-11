"""A tiny in-memory stand-in for the Roblox APIs, used through httpx.MockTransport."""

import json

import httpx

from .conftest import RED_CAP_ECONOMY, RED_CAP_MARKETPLACE, RESELLER_ROW


class FakeRoblox:
    def __init__(self):
        self.csrf = "token-1"
        self.requests: list[httpx.Request] = []
        self.purchases: list[dict] = []
        self.robux = 5000
        self.owned = 0
        self.purchase_status = 200
        self.purchase_body = {"purchased": True}
        self.fail_network_once = False
        self.economy = RED_CAP_ECONOMY
        self.reseller = RESELLER_ROW
        self.marketplace = RED_CAP_MARKETPLACE

    def catalog_row(self, asset_id):
        if asset_id != self.economy["AssetId"]:
            return None
        return {
            "id": asset_id, "itemType": "Asset", "name": self.economy["Name"],
            "itemRestrictions": ["Limited"], "creatorType": "User", "creatorTargetId": 1,
            "price": 7, "lowestResalePrice": 1400, "unitsAvailableForConsumption": 0,
            "collectibleItemId": self.economy["CollectibleItemId"], "hasResellers": True, "isOffSale": True,
        }

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method != "GET" and request.headers.get("x-csrf-token") != self.csrf:
            return httpx.Response(403, headers={"x-csrf-token": self.csrf}, json={"errors": [{"message": "XSRF token invalid"}]})
        if path == "/v1/users/authenticated":
            if ".ROBLOSECURITY" not in request.headers.get("cookie", ""):
                return httpx.Response(401, json={"errors": [{"message": "Unauthorized"}]})
            return httpx.Response(200, json={"id": 42, "name": "tester"})
        if path.endswith("/currency"):
            return httpx.Response(200, json={"robux": self.robux})
        if path == "/v1/catalog/items/details":
            ids = [i["id"] for i in json.loads(request.content).get("items", [])]
            rows = [self.catalog_row(i) for i in ids]
            return httpx.Response(200, json={"data": [r for r in rows if r]})
        if path == "/marketplace-items/v1/items/details":
            ids = json.loads(request.content).get("itemIds", [])
            return httpx.Response(200, json=[self.marketplace] if self.marketplace["collectibleItemId"] in ids else [])
        if path.startswith("/v2/assets/"):
            return httpx.Response(200, json=self.economy)
        if path.endswith("/resellers"):
            return httpx.Response(200, json={"data": [self.reseller] if self.reseller else []})
        if path.endswith("/resellable-instances"):
            inst = [{"serialNumber": i + 1} for i in range(self.owned)]
            return httpx.Response(200, json={"itemInstances": inst, "nextPageCursor": None})
        if path.endswith("/purchase-item") or path.endswith("/purchase-resale"):
            if self.fail_network_once:
                self.fail_network_once = False
                raise httpx.ConnectError("boom", request=request)
            body = json.loads(request.content)
            self.purchases.append({"path": path, **body})
            if self.purchase_status == 200 and self.purchase_body.get("purchased"):
                self.robux -= body["expectedPrice"]
                self.owned += 1
            return httpx.Response(self.purchase_status, json=self.purchase_body)
        return httpx.Response(404, json={})
