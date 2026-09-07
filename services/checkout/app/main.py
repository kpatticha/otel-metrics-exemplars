"""checkout-service -- the edge service.

Every request it serves makes a real HTTP call to inventory-service, so traces
span two services and the latency it records includes a real network hop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Response
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from otel_common import configure_telemetry, shutdown_telemetry

from .inventory_client import InventoryClient
from .work import derive_payment_token

SERVICE_NAME = "checkout-service"
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "1.0.0")

configure_telemetry(SERVICE_NAME, SERVICE_VERSION)
# Instrument the client library before any client is constructed.
HTTPXClientInstrumentor().instrument()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] "
    "trace_id=%(otelTraceID)s span_id=%(otelSpanID)s %(message)s",
)
logger = logging.getLogger(SERVICE_NAME)

tracer = trace.get_tracer(SERVICE_NAME, SERVICE_VERSION)
meter = metrics.get_meter(SERVICE_NAME, SERVICE_VERSION)

orders_created = meter.create_counter(
    "orders.created",
    unit="{order}",
    description="Orders successfully placed",
)
order_value = meter.create_histogram(
    "order.value",
    unit="{USD}",
    description="Monetary value of placed orders",
)
order_rejections = meter.create_counter(
    "orders.rejected",
    unit="{order}",
    description="Orders rejected, by reason",
)

inventory = InventoryClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("checkout ready; inventory at %s", os.getenv("INVENTORY_BASE_URL"))
    try:
        yield
    finally:
        await inventory.close()
        shutdown_telemetry()


app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/products")
async def products(page_size: int = Query(default=5, ge=1, le=50)):
    response = await inventory.catalog(page_size)
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="inventory catalog unavailable")
    return Response(content=response.content, media_type="application/json")


@app.get("/products/{sku}")
async def product(sku: str):
    response = await inventory.stock(sku)
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail=f"unknown sku {sku}")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="inventory unavailable")
    return response.json()


@app.post("/orders")
async def create_order(payload: dict):
    sku = payload.get("sku")
    units = payload.get("units", 1)

    if not isinstance(sku, str) or not isinstance(units, int) or units < 1:
        order_rejections.add(1, {"reason": "invalid_payload"})
        raise HTTPException(status_code=400, detail="sku (str) and units (int >= 1) required")

    order_id = str(uuid.uuid4())
    span = trace.get_current_span()
    span.set_attribute("order.id", order_id)
    span.set_attribute("order.sku", sku)
    span.set_attribute("order.units", units)

    reservation = await inventory.reserve(sku, units)

    if reservation.status_code == 409:
        # The downstream ledger really is out of stock.
        order_rejections.add(1, {"reason": "out_of_stock", "sku": sku})
        raise HTTPException(status_code=409, detail=reservation.json().get("detail"))
    if reservation.status_code == 404:
        order_rejections.add(1, {"reason": "unknown_sku"})
        raise HTTPException(status_code=404, detail=f"unknown sku {sku}")
    if reservation.status_code != 200:
        order_rejections.add(1, {"reason": "inventory_error"})
        raise HTTPException(status_code=502, detail="inventory unavailable")

    # Tokenize the payment. Real key derivation, cost scaling with order size.
    with tracer.start_as_current_span("tokenize_payment") as token_span:
        token = await asyncio.to_thread(derive_payment_token, order_id, sku, units)
        token_span.set_attribute("payment.token_length", len(token))

    price_response = await inventory.stock(sku)
    price_cents = price_response.json().get("price_cents", 0) if price_response.status_code == 200 else 0
    total = (price_cents * units) / 100

    # Recorded under the sampled server span, so both of these carry exemplars
    # pointing at this order's trace.
    orders_created.add(1, {"sku": sku})
    order_value.record(total, {"sku": sku})

    logger.info("order %s placed: %d x %s = %.2f", order_id, units, sku, total)

    return {
        "order_id": order_id,
        "sku": sku,
        "units": units,
        "total": total,
        "payment_token": token[:32],
    }


FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz")
