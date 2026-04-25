"""FastAPI exception handlers.

All ``ProviderError`` subclasses convert to OpenAI-compatible error bodies so
clients written against OpenAI's SDK get the shape they expect. Non-provider
errors fall through to FastAPI defaults.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pronaos.logging import get_logger
from pronaos.providers.base import ProviderError

log = get_logger(__name__)


def _openai_shaped(message: str, err_type: str, status: int) -> dict[str, object]:
    return {
        "error": {
            "message": message,
            "type": err_type,
            "code": status,
        }
    }


async def _provider_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Narrowed before install; runtime cast for mypy-happy type.
    assert isinstance(exc, ProviderError)
    log.warning(
        "provider.error",
        status=exc.status,
        retryable=exc.retryable,
        type=exc.__class__.__name__,
        path=request.url.path,
    )
    return JSONResponse(
        status_code=exc.status,
        content=_openai_shaped(
            message=str(exc),
            err_type=exc.__class__.__name__,
            status=exc.status,
        ),
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ProviderError, _provider_error_handler)
