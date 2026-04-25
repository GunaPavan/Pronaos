"""Structured logging via structlog.

Logs are JSON in production and human-readable in development. A correlation id
(`request_id`) is bound per request by the FastAPI middleware and flows through
every log line and OTEL span for that request.
"""

from __future__ import annotations

import logging
import sys
from typing import cast

import structlog

from pronaos.config import Environment, LogLevel, get_settings


def configure_logging() -> None:
    """Configure structlog for the process. Call once at startup."""
    settings = get_settings()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=_level(settings.log_level),
    )

    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.env is Environment.development:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(_level(settings.log_level)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _level(level: LogLevel) -> int:
    return {
        LogLevel.debug: logging.DEBUG,
        LogLevel.info: logging.INFO,
        LogLevel.warning: logging.WARNING,
        LogLevel.error: logging.ERROR,
    }[level]


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    # structlog.get_logger is dynamically typed — cast so callers get proper
    # .bind/.info/.error/... typing without sprinkling Any everywhere.
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))
