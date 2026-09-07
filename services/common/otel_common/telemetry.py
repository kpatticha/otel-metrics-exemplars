"""OpenTelemetry bootstrap shared by both services.

Everything configurable is driven by the spec's standard environment variables
(https://opentelemetry.io/docs/specs/otel/configuration/sdk-environment-variables/),
so pointing the services at a different backend, changing temporality, or
turning exemplars on and off never requires a code change:

    OTEL_EXPORTER_OTLP_ENDPOINT
    OTEL_EXPORTER_OTLP_HEADERS
    OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta
    OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION=base2_exponential_bucket_histogram
    OTEL_METRICS_EXEMPLAR_FILTER=trace_based
    OTEL_METRIC_EXPORT_INTERVAL
    OTEL_TRACES_SAMPLER / OTEL_TRACES_SAMPLER_ARG
    OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES
    OTEL_SEMCONV_STABILITY_OPT_IN=http

The exemplar chain this sets up:

1. A request arrives, the FastAPI instrumentation starts a **sampled** server
   span and makes it the active span for the duration of the handler.
2. Any measurement recorded while that span is active -- whether by the
   instrumentation itself or by application code -- is offered to the
   instrument's exemplar reservoir.
3. ``OTEL_METRICS_EXEMPLAR_FILTER=trace_based`` accepts a measurement as an
   exemplar candidate when it was recorded under a sampled span, and the SDK
   stamps the exemplar with that span's trace id and span id.
4. On export, each data point carries its reservoir's exemplars, giving a
   backend a direct pointer from an aggregated bucket to a real trace.
"""

from __future__ import annotations

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .partial_success import PartialSuccessLoggingMetricExporter

logger = logging.getLogger(__name__)

_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None

_EXEMPLAR_FILTER_ENV = "OTEL_METRICS_EXEMPLAR_FILTER"

_ENDPOINT_ENVS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
)
_LOCALHOST_PREFIXES = ("http://localhost", "https://localhost", "http://127.0.0.1", "https://127.0.0.1")


def _in_container() -> bool:
    return os.path.exists("/.dockerenv")


def _rewrite_localhost_endpoints() -> None:
    """Make ``localhost`` in an endpoint mean the host, when we are in a container.

    Everyone writes ``http://localhost:9200`` -- it is what works from a shell
    and what the Elasticsearch docs show. Inside a container that address is
    the container itself, so exports fail with connection refused against a
    port nothing is listening on. Rather than demand the reader think in
    container addresses, translate: localhost becomes host.docker.internal,
    which compose maps to the host gateway on Linux too.

    Set an explicit host.docker.internal (or a real hostname) to bypass this
    entirely; it only ever touches loopback addresses.
    """
    if not _in_container():
        return
    for var in _ENDPOINT_ENVS:
        value = os.getenv(var)
        if not value:
            continue
        for prefix in _LOCALHOST_PREFIXES:
            if value.startswith(prefix):
                scheme, _, rest = value.partition("://")
                _, _, tail = rest.partition("/")
                _, _, port = rest.partition(":")
                port = port.split("/", 1)[0]
                host = "host.docker.internal" + (f":{port}" if port else "")
                rewritten = f"{scheme}://{host}" + (f"/{tail}" if tail else "")
                logger.warning(
                    "%s pointed at %s, which inside a container is the container "
                    "itself; using %s instead",
                    var,
                    value,
                    rewritten,
                )
                os.environ[var] = rewritten
                break


def _normalize_exemplar_filter() -> None:
    """Accept ALWAYS_ON as well as always_on.

    The Python SDK compares this variable case-sensitively and raises
    ``ValueError: Unknown exemplar filter 'ALWAYS_ON'`` on anything but lower
    case, which is a hard startup failure. The spec writes the values in lower
    case, but upper case is common in the wild, so normalize rather than crash.
    """
    raw = os.getenv(_EXEMPLAR_FILTER_ENV)
    if raw is None:
        return
    normalized = raw.strip().lower()
    if normalized != raw:
        logger.info(
            "normalized %s from %r to %r", _EXEMPLAR_FILTER_ENV, raw, normalized
        )
        os.environ[_EXEMPLAR_FILTER_ENV] = normalized


def _resource(service_name: str, service_version: str) -> Resource:
    # Resource.create merges these with OTEL_RESOURCE_ATTRIBUTES and
    # OTEL_SERVICE_NAME, so deployment metadata can be injected per environment
    # without touching the image.
    return Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
            "deployment.environment.name": os.getenv("DEPLOYMENT_ENVIRONMENT", "local"),
        }
    )


def configure_telemetry(service_name: str, service_version: str = "1.0.0") -> None:
    """Install global tracer and meter providers. Call once, before serving."""
    global _tracer_provider, _meter_provider

    if _tracer_provider is not None:
        return

    _rewrite_localhost_endpoints()
    resource = _resource(service_name, service_version)

    # -- Traces ---------------------------------------------------------------
    # Sampling is left to OTEL_TRACES_SAMPLER (parentbased_traceidratio locally
    # at ratio 1.0). This matters for exemplars: under the trace_based filter,
    # an unsampled span yields no exemplar, so the sampler is effectively an
    # exemplar volume control too.
    _tracer_provider = TracerProvider(resource=resource)
    _tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(_tracer_provider)

    # Injects otelTraceID / otelSpanID onto every LogRecord, including records
    # from libraries, so log lines can be correlated with traces.
    #
    # inject_trace_context=True is required: the instrumentation's record
    # factory adds those fields only when it is set, and set_logging_format
    # alone does not imply it. Without it, any log format referencing
    # otelTraceID raises KeyError on every record. set_logging_format stays
    # False so each service keeps its own format.
    LoggingInstrumentor().instrument(
        set_logging_format=False,
        inject_trace_context=True,
    )

    # -- Metrics --------------------------------------------------------------
    # Temporality and histogram aggregation come from the standard env vars, so
    # the delta + base2 exponential histogram combination that Elasticsearch
    # TSDB wants is configuration, not code.
    _normalize_exemplar_filter()
    reader = PeriodicExportingMetricReader(PartialSuccessLoggingMetricExporter())
    _meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(_meter_provider)

    logger.info(
        "telemetry configured service=%s endpoint=%s exemplar_filter=%s "
        "temporality=%s histogram_aggregation=%s",
        service_name,
        os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "(default)"),
        os.getenv("OTEL_METRICS_EXEMPLAR_FILTER", "trace_based (SDK default)"),
        os.getenv("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "(default)"),
        os.getenv("OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION", "(default)"),
    )


def shutdown_telemetry() -> None:
    """Flush and stop both providers so the last interval is not lost."""
    if _meter_provider is not None:
        _meter_provider.shutdown()
    if _tracer_provider is not None:
        _tracer_provider.shutdown()
