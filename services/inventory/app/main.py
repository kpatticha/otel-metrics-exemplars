"""inventory-service -- the downstream service.

Owns the stock ledger and does the CPU-bound work. Called over HTTP by
checkout-service; never calls anything itself, so it is the leaf of every trace.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Response
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from otel_common import configure_telemetry, shutdown_telemetry

from .ledger import OutOfStock, UnknownSku, ledger, restock_loop
from .work import build_catalog_page, derive_stock_token

SERVICE_NAME = "inventory-service"
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "1.0.0")

RESTOCK_INTERVAL_SECONDS = float(os.getenv("RESTOCK_INTERVAL_SECONDS", "20"))
RESTOCK_UNITS_PER_SKU = int(os.getenv("RESTOCK_UNITS_PER_SKU", "25"))

configure_telemetry(SERVICE_NAME, SERVICE_VERSION)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] "
    "trace_id=%(otelTraceID)s span_id=%(otelSpanID)s %(message)s",
)
logger = logging.getLogger(SERVICE_NAME)

tracer = trace.get_tracer(SERVICE_NAME, SERVICE_VERSION)
meter = metrics.get_meter(SERVICE_NAME, SERVICE_VERSION)

# Application instruments. These are recorded inside the request handler, which
# runs under the instrumentation's sampled server span -- so every measurement
# is an exemplar candidate and carries that span's trace id.
reservation_units = meter.create_counter(
    "inventory.reservation.units",
    unit="{unit}",
    description="Units successfully reserved",
)
token_work = meter.create_histogram(
    "inventory.token.derivation.duration",
    unit="s",
    description="Time spent deriving a stock verification token",
)
catalog_bytes = meter.create_histogram(
    "inventory.catalog.page.size",
    unit="By",
    description="Serialized size of a catalog page",
)


def _observe_availability(options: metrics.CallbackOptions):
    # Asynchronous instruments are collected outside any request, so by design
    # these observations carry no exemplars -- there is no span to point at.
    for sku, available in ledger.availability().items():
        yield metrics.Observation(available, {"sku": sku})


meter.create_observable_gauge(
    "inventory.stock.available",
    callbacks=[_observe_availability],
    unit="{unit}",
    description="Currently available units per SKU",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(restock_loop(RESTOCK_INTERVAL_SECONDS, RESTOCK_UNITS_PER_SKU))
    logger.info(
        "inventory ready; restocking %d units/SKU every %.0fs",
        RESTOCK_UNITS_PER_SKU,
        RESTOCK_INTERVAL_SECONDS,
    )
    try:
        yield
    finally:
        task.cancel()
        shutdown_telemetry()


app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/catalog")
async def catalog(page_size: int = Query(default=5, ge=1, le=50)):
    items = ledger.all_items()
    # Repeat the catalog so page_size can exceed the seed catalog length; the
    # point is that a bigger page genuinely costs more to serialize.
    expanded = (items * ((page_size // max(len(items), 1)) + 1))[:page_size]
    body, checksum = await asyncio.to_thread(build_catalog_page, expanded, page_size)

    catalog_bytes.record(len(body), {"page.size": page_size})
    span = trace.get_current_span()
    span.set_attribute("catalog.page_size", page_size)
    span.set_attribute("catalog.checksum", checksum)

    return Response(content=body, media_type="application/json")


@app.get("/stock/{sku}")
async def stock(sku: str):
    try:
        item = ledger.get(sku)
    except UnknownSku:
        raise HTTPException(status_code=404, detail=f"unknown sku {sku}")

    # CPU-bound; offloaded to a worker thread so the event loop stays
    # responsive. contextvars are copied into the thread, so the active span
    # travels with it.
    with tracer.start_as_current_span("derive_stock_token") as span:
        start = asyncio.get_running_loop().time()
        token = await asyncio.to_thread(derive_stock_token, sku)
        elapsed = asyncio.get_running_loop().time() - start
        span.set_attribute("token.length", len(token))

    token_work.record(elapsed, {"sku": sku})

    return {
        "sku": item.sku,
        "name": item.name,
        "price_cents": item.price_cents,
        "available": item.stock - item.reserved,
        "token": token,
    }


@app.post("/reserve")
async def reserve(payload: dict):
    sku = payload.get("sku")
    units = payload.get("units", 1)

    if not isinstance(sku, str) or not isinstance(units, int) or units < 1:
        # A real 400 from a real invalid payload.
        raise HTTPException(status_code=400, detail="sku (str) and units (int >= 1) required")

    try:
        remaining = ledger.reserve(sku, units)
    except UnknownSku:
        raise HTTPException(status_code=404, detail=f"unknown sku {sku}")
    except OutOfStock as exc:
        # 409 because the ledger actually ran out, not because we decided to
        # fail this request.
        span = trace.get_current_span()
        span.set_attribute("inventory.requested_units", exc.requested)
        span.set_attribute("inventory.available_units", exc.available)
        logger.info("reservation rejected for %s: %s", sku, exc)
        raise HTTPException(status_code=409, detail=str(exc))

    reservation_units.add(units, {"sku": sku})
    return {"sku": sku, "reserved": units, "remaining": remaining}


FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz")
