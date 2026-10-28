from fastapi import APIRouter, Depends

from app.api.security import require_api_key
from app.api.v1.endpoints import extraction, health, matching

api_router = APIRouter()
api_router.include_router(health.router)  # open: used by monitoring
api_router.include_router(extraction.router, dependencies=[Depends(require_api_key)])
api_router.include_router(matching.router, dependencies=[Depends(require_api_key)])
