"""Real CPU work performed by checkout-service."""

from __future__ import annotations

import hashlib
import os

# Payment tokenization is genuinely expensive work, and the cost scales with
# the order -- more units means more rounds. That gives POST /orders a latency
# distribution driven by order shape rather than by a random number generator.
_ROUNDS_PER_UNIT = int(os.getenv("PAYMENT_ROUNDS_PER_UNIT", "2600"))
_BASE_ROUNDS = int(os.getenv("PAYMENT_BASE_ROUNDS", "4000"))


def derive_payment_token(order_id: str, sku: str, units: int) -> str:
    material = f"{order_id}:{sku}:{units}".encode("utf-8")
    rounds = _BASE_ROUNDS + _ROUNDS_PER_UNIT * units
    return hashlib.pbkdf2_hmac("sha256", material, b"checkout-payments", rounds).hex()
