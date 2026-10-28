"""Use case: match cheque files against expected payments (POST /cheques/match)."""

import asyncio
import logging
import time
import uuid
from typing import Optional, Union

import httpx

from app.core.config import Settings
from app.core.exceptions import AppError, InvalidRequestError
from app.core.logging import log_result
from app.schemas import FileResult, MatchRequest, RentPayment, TokenUsage
from app.services.cheque_extractor import ChequeExtractor
from app.services.cheque_imaging import attach_cheque_files
from app.services.file_loader import file_name_from_source, load_file, redact
from app.services.payment_matcher import Match, match_payments
from app.utils.files import UploadedFile

logger = logging.getLogger(__name__)


async def match_cheques(
    request: MatchRequest, settings: Settings, extractor: ChequeExtractor
) -> list[RentPayment]:
    """Load + extract every file, then fill MatchedWithOcr / MatchedOcrFileURL / CroppedOCRFileURL / OcrResponse."""
    if not request.payments:
        raise InvalidRequestError("The payment list is empty.")
    if not request.file_urls:
        raise InvalidRequestError("No file URLs provided.")
    if len(request.file_urls) > settings.max_files_per_request:
        raise InvalidRequestError(
            f"Too many files. Send at most {settings.max_files_per_request} file URLs per request."
        )

    request_id = uuid.uuid4().hex[:12]
    start = time.perf_counter()
    sources, payments = request.file_urls, request.payments
    logger.info("[%s] Match request: %d file(s), %d payment(s), model=%s",
                request_id, len(sources), len(payments), settings.gemini_model)

    results, uploads = await _extract_all(sources, settings, extractor, request_id)
    matches = match_payments(payments, list(zip(sources, results)))
    await _crop_matched(matches, sources, results, uploads, settings, request_id)

    _log(request_id, settings, sources, results, payments, start)
    return payments


async def _extract_all(
    sources: list[str], settings: Settings, extractor: ChequeExtractor, request_id: str
) -> tuple[list[FileResult], dict[int, UploadedFile]]:
    """One FileResult per source (same order as `sources`), plus the loaded files by index."""
    async with httpx.AsyncClient(timeout=settings.download_timeout_seconds) as client:
        loaded = await asyncio.gather(*(_load(s, client, settings, request_id) for s in sources))

    results: list[Optional[FileResult]] = [r if isinstance(r, FileResult) else None for r in loaded]
    uploads = {i: f for i, f in enumerate(loaded) if isinstance(f, UploadedFile)}
    extracted = await extractor.extract_many(list(uploads.values()))
    for i, result in zip(uploads, extracted):
        results[i] = result
    return results, uploads


async def _crop_matched(
    matches: list[Match], sources: list[str], results: list[FileResult],
    uploads: dict[int, UploadedFile], settings: Settings, request_id: str,
) -> None:
    """Crop / rotate only the matched cheques and set CroppedOCRFileURL on their payments."""
    wanted: dict[int, set[int]] = {}
    for m in matches:
        wanted.setdefault(m.file_index, set()).add(m.cheque_index)

    await asyncio.gather(*(
        attach_cheque_files(sources[f], uploads[f], results[f], settings, request_id, only=indexes)
        for f, indexes in wanted.items()
    ))
    for m in matches:
        cropped = results[m.file_index].cheque_files[m.cheque_index]
        m.payment.CroppedOCRFileURL = cropped if cropped != sources[m.file_index] else None


async def _load(
    source: str, client: httpx.AsyncClient, settings: Settings, request_id: str
) -> Union[UploadedFile, FileResult]:
    """Load + validate one file; a failure becomes an error FileResult."""
    try:
        return await load_file(source, client, settings)
    except AppError as exc:
        logger.warning("[%s] Could not load %s: %s",
                       request_id, redact(source), exc.__cause__ or exc.message)
        return FileResult(file_name=file_name_from_source(source), status="error", error=exc.message)


def _log(
    request_id: str,
    settings: Settings,
    sources: list[str],
    results: list[FileResult],
    payments: list[RentPayment],
    start: float,
) -> None:
    matched = sum(bool(p.MatchedWithOcr) for p in payments)
    usage = TokenUsage.total(results)
    elapsed_ms = int((time.perf_counter() - start) * 1000)

    for p in payments:
        logger.info("[%s] Payment %s (inst %s, %s, %s, %s): matched=%s file=%s cropped=%s | %s",
                    request_id, p.RentPaymentId, p.Installment, p.ServiceDescription,
                    p.Amount, p.DueDate, p.MatchedWithOcr,
                    redact(p.MatchedOcrFileURL or "-"), redact(p.CroppedOCRFileURL or "-"),
                    p.OcrResponse)
    logger.info(
        "[%s] Match completed: %d/%d payments matched | files ok=%d/%d | "
        "tokens: prompt=%d output=%d thinking=%d total=%d | %d ms",
        request_id, matched, len(payments),
        sum(r.status == "success" for r in results), len(results),
        usage.prompt_tokens, usage.output_tokens, usage.thinking_tokens, usage.total_tokens,
        elapsed_ms,
    )
    log_result({
        "request_id": request_id,
        "type": "match",
        "model": settings.gemini_model,
        "matched": matched,
        "unmatched": len(payments) - matched,
        "usage": usage.model_dump(),
        "processing_time_ms": elapsed_ms,
        "files": [{"source": redact(s), **r.log_entry()} for s, r in zip(sources, results)],
        "payments": [
            {**p.model_dump(),
             "MatchedOcrFileURL": redact(p.MatchedOcrFileURL) if p.MatchedOcrFileURL else None,
             "CroppedOCRFileURL": redact(p.CroppedOCRFileURL) if p.CroppedOCRFileURL else None}
            for p in payments
        ],
    })
