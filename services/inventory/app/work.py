"""Real work, so latency is measured rather than invented.

Nothing here sleeps or samples from a distribution. Every millisecond a request
spends in these functions is millisecond spent computing something: deriving a
key, serializing a payload, checksumming bytes. The latency histograms the
services export are therefore a genuine measurement of genuine work, and the
exemplars attached to them point at traces of requests that really did take
that long.

Cost varies per request because the *input* varies -- different SKUs derive
different key lengths, different page sizes serialize different payloads. That
is real request diversity, not a synthetic latency profile.
"""

from __future__ import annotations

import hashlib
import json
import zlib

# Key derivation rounds are deliberately in a range where the cost is visible
# in a latency histogram (single-digit to low tens of milliseconds) without
# making the service useless under load.
_MIN_ROUNDS = 1_200
_ROUND_SPREAD = 9_000


def _rounds_for(key: str) -> int:
    """Derive a stable, per-key round count.

    Because it is derived from the key rather than drawn at random, the same SKU
    always costs the same amount of work -- so a slow series stays slow, the way
    a genuinely expensive code path would.
    """
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=4).digest()
    return _MIN_ROUNDS + int.from_bytes(digest, "big") % _ROUND_SPREAD


def derive_stock_token(sku: str, salt: bytes = b"inventory-stock") -> str:
    """Derive a verification token for a SKU. Genuinely CPU-bound."""
    token = hashlib.pbkdf2_hmac("sha256", sku.encode("utf-8"), salt, _rounds_for(sku))
    return token.hex()


def build_catalog_page(items: list[dict], page_size: int) -> tuple[str, int]:
    """Serialize a catalog page and checksum it.

    Returns the JSON body and its CRC32. Larger pages cost proportionally more
    to serialize, which is why ``GET /catalog`` latency tracks ``page_size``.
    """
    page = items[:page_size]
    body = json.dumps({"items": page, "count": len(page)}, separators=(",", ":"))
    checksum = zlib.crc32(body.encode("utf-8"))
    return body, checksum
