from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_exception_handlers
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.logging import setup_logging


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level, settings.logs_dir)

    docs = settings.docs_enabled
    app = FastAPI(
        title=settings.app_name,
        description="Extracts cheque details and matches cheques to expected rent payments.",
        version=settings.app_version,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    register_exception_handlers(app)
    _enable_swagger_file_picker(app)
    return app


def _enable_swagger_file_picker(app: FastAPI) -> None:
    """OpenAPI 3.1 marks uploads as `contentMediaType`, which Swagger UI renders as
    text inputs. Adding `format: binary` restores the "Choose file" button."""
    default_openapi = app.openapi

    def _patch(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "string" and "contentMediaType" in node:
                node["format"] = "binary"
            for value in node.values():
                _patch(value)
        elif isinstance(node, list):
            for item in node:
                _patch(item)

    def custom_openapi() -> dict:
        if app.openapi_schema is None:
            _patch(default_openapi())
        return app.openapi_schema

    app.openapi = custom_openapi


app = create_app()
