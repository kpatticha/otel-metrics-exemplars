"""Surface OTLP partial-success responses instead of swallowing them.

The OTLP spec lets a server accept a request while reporting that it dropped
some data, via the ``partial_success`` field on the export response. The SDK's
HTTP exporter treats any 2xx as a clean success and never looks at the body, so
those warnings are invisible.

That matters here. Elasticsearch's exemplar ingestion deduplicates exemplars
that share a metric name, timestamp and dimension set -- and because exemplar
``@timestamp`` is truncated to milliseconds on ingest, a busy service naturally
produces collisions. Elasticsearch reports those drops as a partial success with
``rejected_data_points: 0`` plus a warning message, which is exactly the signal
you want when testing the ingest path.

This wrapper subclasses the stock exporter and parses the response body. It
reaches into ``_export``, a private method, so it is written to degrade
gracefully: any incompatibility logs once and leaves exporting unaffected.
"""

from __future__ import annotations

import logging

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceResponse,
)

logger = logging.getLogger(__name__)


class PartialSuccessLoggingMetricExporter(OTLPMetricExporter):
    """OTLP/HTTP metric exporter that logs ``partial_success`` from responses."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._introspection_failed = False

    def _export(self, *args, **kwargs):
        response = super()._export(*args, **kwargs)
        try:
            self._log_partial_success(response)
        except Exception:  # noqa: BLE001 - never break exporting to report on it
            if not self._introspection_failed:
                self._introspection_failed = True
                logger.warning(
                    "Could not inspect OTLP response for partial_success; "
                    "continuing without partial-success reporting",
                    exc_info=True,
                )
        return response

    @staticmethod
    def _log_partial_success(response) -> None:
        status = getattr(response, "status_code", None)
        content = getattr(response, "content", None)
        if status is None or not (200 <= status < 300) or not content:
            return

        parsed = ExportMetricsServiceResponse()
        parsed.ParseFromString(content)
        if not parsed.HasField("partial_success"):
            return

        partial = parsed.partial_success
        rejected = partial.rejected_data_points
        message = partial.error_message

        # A partial success with zero rejected points is the spec's way of
        # returning a warning, not an error -- Elasticsearch uses it for
        # dropped duplicate exemplars.
        if rejected:
            logger.warning(
                "OTLP partial success: backend rejected %d data point(s): %s",
                rejected,
                message or "(no message)",
            )
        elif message:
            logger.warning("OTLP warning from backend: %s", message)
