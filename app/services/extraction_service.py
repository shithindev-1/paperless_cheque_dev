"""Use case: extract cheque data from uploaded files (POST /cheques/extract)."""

import logging
import uuid

from fastapi import UploadFile

from app.core.config import Settings
from app.core.exceptions import FileValidationError, InvalidRequestError
from app.core.logging import log_result
from app.schemas import BatchResponse, FileResult, TokenUsage
from app.services.cheque_extractor import ChequeExtractor
from app.utils.files import UploadedFile, read_and_validate

logger = logging.getLogger(__name__)


async def extract_uploads(
    uploads: list[UploadFile], settings: Settings, extractor: ChequeExtractor
) -> BatchResponse:
    if len(uploads) > settings.max_files_per_request:
        raise InvalidRequestError(
            f"Too many files. Upload at most {settings.max_files_per_request} files per request."
        )

    request_id = uuid.uuid4().hex[:12]
    logger.info("[%s] Request received: %d file(s), model=%s",
                request_id, len(uploads), settings.gemini_model)

    valid: list[UploadedFile] = []
    rejected: list[FileResult] = []
    for upload in uploads:
        try:
            valid.append(await read_and_validate(upload, settings))
        except FileValidationError as exc:
            logger.warning("[%s] Rejected %s: %s", request_id, upload.filename, exc.message)
            rejected.append(FileResult(
                file_name=upload.filename or "unnamed", status="error", error=exc.message
            ))

    results = await extractor.extract_many(valid) + rejected
    successful = sum(r.status == "success" for r in results)
    usage = TokenUsage.total(results)

    logger.info(
        "[%s] Completed: %d/%d succeeded | tokens: prompt=%d output=%d thinking=%d total=%d",
        request_id, successful, len(results), usage.prompt_tokens,
        usage.output_tokens, usage.thinking_tokens, usage.total_tokens,
    )
    log_result({
        "request_id": request_id,
        "type": "extract",
        "model": settings.gemini_model,
        "successful": successful,
        "failed": len(results) - successful,
        "usage": usage.model_dump(),
        "results": [r.log_entry() for r in results],
    })
    return BatchResponse(results=results)
