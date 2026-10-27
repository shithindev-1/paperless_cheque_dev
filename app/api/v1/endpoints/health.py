from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health(settings: Settings = Depends(get_settings)) -> dict:
    """Liveness check. Deliberately exposes no model or configuration details."""
    return {"status": "ok", "version": settings.app_version}
