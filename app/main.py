import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_exception_handlers
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import log_result, setup_logging
from app.core.tracing import RequestTrace


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
        expose_headers=["X-Request-ID"],
    )
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    register_exception_handlers(app)
    _trace_requests(app, settings)
    _enable_swagger_file_picker(app)
    return app


def _trace_requests(app: FastAPI, settings: Settings) -> None:
    """One trace per cheque API hit: written once at the end, id returned as X-Request-ID."""
    traced_prefix = f"{settings.api_v1_prefix}/cheques"
    app_info = {"version": settings.app_version, "env": settings.app_env,
                "model": settings.gemini_model}

    @app.middleware("http")
    async def trace(request: Request, call_next):
        if not request.url.path.startswith(traced_prefix):
            return await call_next(request)
        trace = RequestTrace(request.method, request.url.path,
                             request.client.host if request.client else None,
                             request.headers.get("user-agent"), app_info)
        token = trace.activate()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = trace.request_id
            return response
        except Exception as exc:  # unhandled: the 500 handler runs outside this trace
            trace.error(type(exc).__name__, str(exc)[:500])
            raise
        finally:
            log_result(trace.finish(status))
            logging.getLogger("app.request").info("[%s] SUMMARY %s", trace.request_id,
                                                  trace.summary_line())
            trace.deactivate(token)


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
