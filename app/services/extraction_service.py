"""Use case: extract cheque data from uploaded files (POST /cheques/extract).

Stages (each timed in the request trace): validation -> extraction.
"""

import dataclasses
import logging
from collections import Counter

from fastapi import UploadFile

from app.core.config import Settings
from app.core.exceptions import FileValidationError, InvalidRequestError
from app.core.tracing import RequestTrace, current_trace
from app.schemas import BatchResponse, FileResult, TokenUsage
from app.services.cheque_extractor import ChequeExtractor
from app.utils.files import UploadedFile, read_and_validate

logger = logging.getLogger(__name__)


async def extract_uploads(
    uploads: list[UploadFile], settings: Settings, extractor: ChequeExtractor
) -> BatchResponse:
    trace = current_trace() or RequestTrace("POST", "extract", None, None, {})
    trace.set(type="extract")
    trace.count(files=len(uploads))
    if len(uploads) > settings.max_files_per_request:
        raise InvalidRequestError(
            f"Too many files. Upload at most {settings.max_files_per_request} files per request."
        )

    request_id = trace.request_id
    logger.info("[%s] Request received: %d file(s), model=%s",
                request_id, len(uploads), settings.gemini_model)

    valid: list[UploadedFile] = []
    rejected: list[FileResult] = []
    with trace.stage("validation"):
        for upload in uploads:
            try:
                file = await read_and_validate(upload, settings)
                valid.append(dataclasses.replace(file, trace=trace.new_file(
                    name=file.name, mime_type=file.mime_type, size_kb=round(len(file.data) / 1024, 1))))
            except FileValidationError as exc:
                logger.warning("[%s] Rejected %s: %s", request_id, upload.filename, exc.message)
                trace.new_file(name=upload.filename or "unnamed", status="rejected", error=exc.message)
                rejected.append(FileResult(
                    file_name=upload.filename or "unnamed", status="error", error=exc.message
                ))

    with trace.stage("extraction"):
        results = await extractor.extract_many(valid) + rejected
    successful = sum(r.status == "success" for r in results)
    usage = TokenUsage.total(results)
    cheques = [c for r in results for c in r.cheques]

    logger.info("[%s] Completed: %d/%d succeeded | tokens=%d | %d ms",
                request_id, successful, len(results), usage.total_tokens, trace.elapsed_ms())
    trace.count(files_ok=successful, files_failed=len(results) - successful,
                cheques_found=len(cheques))
    trace.set(usage=usage.model_dump(),
              issues_summary=dict(Counter(i.code.value for c in cheques for i in c.issues)))
    return BatchResponse(results=results)
