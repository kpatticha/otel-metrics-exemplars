"""An in-memory stock ledger that genuinely runs out.

Errors in this demo are real outcomes of real state, not injected failures. A
reservation fails with 409 because the stock actually reached zero, and it
recovers when the restock task actually puts units back. The error-rate metrics
and the exemplars on them therefore describe something that happened.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass


@dataclass
class Item:
    sku: str
    name: str
    price_cents: int
    stock: int
    reserved: int = 0


class OutOfStock(Exception):
    def __init__(self, sku: str, requested: int, available: int):
        super().__init__(f"{sku}: requested {requested}, available {available}")
        self.sku = sku
        self.requested = requested
        self.available = available


class UnknownSku(Exception):
    def __init__(self, sku: str):
        super().__init__(sku)
        self.sku = sku


# A small catalog with deliberately uneven stock, so some SKUs deplete under
# load and others never do. That asymmetry is what makes the error-rate series
# interesting without anyone injecting failures.
_SEED = [
    Item("SKU-1001", "Mechanical keyboard", 12900, stock=500),
    Item("SKU-1002", "27-inch monitor", 34900, stock=180),
    Item("SKU-1003", "USB-C dock", 19900, stock=90),
    Item("SKU-1004", "Noise-cancelling headphones", 27900, stock=40),
    Item("SKU-1005", "Limited-edition desk mat", 4900, stock=8),
]


class Ledger:
    def __init__(self) -> None:
        self._items: dict[str, Item] = {i.sku: Item(**vars(i)) for i in _SEED}
        self._lock = threading.Lock()

    def all_items(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "sku": i.sku,
                    "name": i.name,
                    "price_cents": i.price_cents,
                    "available": i.stock - i.reserved,
                }
                for i in self._items.values()
            ]

    def get(self, sku: str) -> Item:
        with self._lock:
            item = self._items.get(sku)
            if item is None:
                raise UnknownSku(sku)
            return Item(**vars(item))

    def reserve(self, sku: str, units: int) -> int:
        """Reserve units, or raise OutOfStock. Returns remaining availability."""
        with self._lock:
            item = self._items.get(sku)
            if item is None:
                raise UnknownSku(sku)
            available = item.stock - item.reserved
            if units > available:
                raise OutOfStock(sku, units, available)
            item.reserved += units
            return item.stock - item.reserved

    def restock(self, units_per_sku: int) -> None:
        """Release reservations so the demo can run indefinitely."""
        with self._lock:
            for item in self._items.values():
                item.reserved = max(0, item.reserved - units_per_sku)

    def availability(self) -> dict[str, int]:
        with self._lock:
            return {i.sku: i.stock - i.reserved for i in self._items.values()}


ledger = Ledger()


async def restock_loop(interval_seconds: float, units_per_sku: int) -> None:
    """Periodically release reservations. Keeps the 409 path cyclical."""
    while True:
        await asyncio.sleep(interval_seconds)
        ledger.restock(units_per_sku)
