"""HTTP client for inventory-service.

httpx is instrumented, so each call here produces a real client span that is a
child of the incoming server span, and contributes to
``http.client.request.duration``. Trace context is propagated over the wire via
W3C traceparent, which is what makes an exemplar recorded in checkout resolve to
a trace that also contains inventory's spans.
"""

from __future__ import annotations

import os

import httpx

INVENTORY_BASE_URL = os.getenv("INVENTORY_BASE_URL", "http://inventory:8000")
TIMEOUT_SECONDS = float(os.getenv("INVENTORY_TIMEOUT_SECONDS", "10"))


class InventoryClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=INVENTORY_BASE_URL,
            timeout=TIMEOUT_SECONDS,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def catalog(self, page_size: int) -> httpx.Response:
        return await self._client.get("/catalog", params={"page_size": page_size})

    async def stock(self, sku: str) -> httpx.Response:
        return await self._client.get(f"/stock/{sku}")

    async def reserve(self, sku: str, units: int) -> httpx.Response:
        return await self._client.post("/reserve", json={"sku": sku, "units": units})
