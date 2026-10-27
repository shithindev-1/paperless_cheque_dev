"""Global exception handlers — every error response is `{"error": "<general message>"}`."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppError

logger = logging.getLogger(__name__)


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    logger.warning("%s %s -> %s: %s", request.method, request.url.path,
                   type(exc).__name__, exc.message)
    return _error(exc.status_code, exc.message)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    logger.warning("%s %s -> invalid request: %s", request.method, request.url.path, exc.errors())
    if request.url.path.endswith("/match"):
        return _error(422, "Invalid request. Send 'file_urls' and 'payments' (each with Amount "
                           "and a valid DueDate) as JSON.")
    return _error(422, "Invalid request. Upload one or more files using the 'files' field.")


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return _error(exc.status_code, str(exc.detail))


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("%s %s -> unhandled error", request.method, request.url.path)
    return _error(500, AppError.message)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
