"""Discord webhook notifications - only to the webhook URL you configure."""

from __future__ import annotations

import asyncio
import logging

import httpx

from . import __version__
from .models import Offer

log = logging.getLogger(__name__)

GREEN, RED, YELLOW = 0x2ECC71, 0xE74C3C, 0xF1C40F


class Notifier:
    def __init__(self, url: str | None, send_fails: bool, ping_user: int = 0):
        self.url = url
        self.send_fails = send_fails
        self.ping_user = ping_user
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=200)
        self.client = httpx.AsyncClient(timeout=10, trust_env=False) if url else None

    def _embed(self, title: str, offer: Offer, color: int, extra: str = "") -> dict:
        desc = f"**Price:** `{offer.price} R$`\n**Type:** `{offer.kind}`"
        if offer.serial:
            desc += f"\n**Serial:** `#{offer.serial}`"
        if extra:
            desc += f"\n{extra}"
        return {
            "title": f"{title}: {offer.name}"[:256],
            "url": f"https://www.roblox.com/catalog/{offer.asset_id}",
            "description": desc[:4000],
            "color": color,
            "footer": {"text": f"Limited Sniper v{__version__}"},
        }

    def _push(self, payload: dict) -> None:
        if not self.client:
            return
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            log.warning("Webhook queue full, dropping a notification")

    def bought(self, offer: Offer, robux_left: int | None) -> None:
        extra = f"**Robux left:** `{robux_left:,}`" if robux_left is not None else ""
        payload = {"embeds": [self._embed("Bought", offer, GREEN, extra)]}
        if self.ping_user:
            payload["content"] = f"<@{self.ping_user}>"
            payload["allowed_mentions"] = {"users": [str(self.ping_user)]}
        self._push(payload)

    def failed(self, offer: Offer, reason: str) -> None:
        if self.send_fails:
            self._push({"embeds": [self._embed("Failed", offer, RED, f"**Reason:** `{reason[:300]}`")]})

    def dry_run(self, offer: Offer) -> None:
        self._push({"embeds": [self._embed("Dry run (not bought)", offer, YELLOW)]})

    def message(self, text: str) -> None:
        self._push({"content": text[:1900], "allowed_mentions": {"parse": []}})

    async def run(self) -> None:
        if not self.client or not self.url:
            return
        while True:
            payload = await self.queue.get()
            for attempt in range(3):
                try:
                    r = await self.client.post(self.url, json=payload)
                    if r.status_code == 429:
                        await asyncio.sleep(float(r.headers.get("retry-after", 2)))
                        continue
                    if r.status_code >= 400:
                        log.warning("Webhook returned HTTP %s", r.status_code)
                    break
                except httpx.HTTPError as e:
                    log.warning("Webhook error: %s", e)
                    await asyncio.sleep(2 * (attempt + 1))

    async def aclose(self) -> None:
        if self.client:
            await self.client.aclose()
