from fastapi import APIRouter

from app.api.v1.endpoints import extraction, health, matching

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(extraction.router)
api_router.include_router(matching.router)
