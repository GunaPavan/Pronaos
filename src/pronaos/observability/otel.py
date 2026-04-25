"""OpenTelemetry setup.

Instruments FastAPI, httpx, SQLAlchemy, and Redis out of the box so every
request produces a full trace tree without per-call instrumentation. Spans are
exported to the configured OTLP endpoint (the collector in local dev, vendor
backend in production).
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from pronaos.config import get_settings


def configure_tracing() -> None:
    """Install the OTEL tracer provider for the process.

    Safe to call multiple times; a no-op when OTEL is disabled or a provider
    is already installed.
    """
    settings = get_settings()
    if not settings.otel_enabled:
        return

    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return  # already configured

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": "0.1.0",
            "deployment.environment": settings.env.value,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_endpoint))
    )
    trace.set_tracer_provider(provider)


def get_tracer(name: str = "pronaos") -> trace.Tracer:
    return trace.get_tracer(name)
