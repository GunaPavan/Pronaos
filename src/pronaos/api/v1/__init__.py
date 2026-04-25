"""v1 public API surface."""

from fastapi import APIRouter

from pronaos.api.v1 import chat, health

router = APIRouter(prefix="/v1")
router.include_router(health.router)
router.include_router(chat.router)

__all__ = ["router"]
