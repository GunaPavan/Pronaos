"""v1 public API surface."""

from fastapi import APIRouter

from pronaos.api.v1 import admin, chat, health

router = APIRouter(prefix="/v1")
router.include_router(health.router)
router.include_router(chat.router)
router.include_router(admin.router)

__all__ = ["router"]
