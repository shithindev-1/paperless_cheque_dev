"""Use case: match cheque files against expected payments.

POST /cheques/match         files given as URLs (blob) or, locally, names inside LOCAL_FILES_DIR
POST /cheques/match/upload  files uploaded in the request (form-data); same checks and response

Stages (each timed in the request trace): load -> extraction -> matching -> cropping.
After the response, files behind failed matches go to the case study (app.services.case_study).
"""

import asyncio
import dataclasses
import logging
import time
from collections import Counter
from typing import Optional, Union

import httpx
from fastapi import BackgroundTasks, UploadFile
from pydantic import TypeAdapter, ValidationError

from app.core.config import Settings
from app.core.exceptions import (
    AppError, ChequeInfoInPaymentError, FileValidationError, InvalidChequeInfoError, InvalidPaymentsError,
    InvalidRequestError,
)
from app.core.tracing import RequestTrace, current_trace
from app.schemas import ChequeInfo, FileResult, MatchRequest, MatchResponse, RentPayment, TokenUsage
from app.schemas.matching import CHEQUE_INFO_IN_PAYMENT, CHEQUE_INFO_KEYS, share_cheque_info
from app.services import case_study
from app.services.cheque_extractor import ChequeExtractor
from app.services.cheque_imaging import attach_cheque_files
from app.services.file_loader import file_name_from_source, load_file, redact
from app.services.payment_matcher import Match, match_payments
from app.utils.files import UploadedFile, read_and_validate

logger = logging.getLogger(__name__)

_PAYMENTS = TypeAdapter(list[RentPayment])


async def match_cheques(
    request: MatchRequest, settings: Settings, extractor: ChequeExtractor,
    background: Optional[BackgroundTasks] = None,
) -> MatchResponse:
    """Load + extract every file, then fill MatchedWithOcr / MatchedOcrFileURL / CroppedOCRFileURL / OcrResponse."""
    trace = current_trace() or RequestTrace("POST", "match", None, None, {})
    trace.set(type="match")
    trace.count(files=len(request.file_urls), payments=len(request.payments))
    _check_counts(len(request.file_urls), request.payments, settings, "file URLs")

    request_id = trace.request_id  # full GUID: also names the cropped-files folder
    sources, payments = request.file_urls, request.payments
    logger.info("[%s] Match request: %d file(s), %d payment(s), model=%s",
                request_id, len(sources), len(payments), settings.gemini_model)

    with trace.stage("load"):
        loaded = await _load_all(sources, settings, trace, request_id)
    payments = await _match(sources, loaded, payments, settings, extractor, trace, background)
    return MatchResponse(cheque_info=request.cheque_info, payments=payments)


async def match_uploads(
    files: list[UploadFile], payments_json: str, settings: Settings, extractor: ChequeExtractor,
    background: Optional[BackgroundTasks] = None, cheque_info_json: Optional[str] = None,
) -> MatchResponse:
    """Same as match_cheques, for files uploaded in the request. MatchedOcrFileURL is the file name."""
    trace = current_trace() or RequestTrace("POST", "match_upload", None, None, {})
    trace.set(type="match_upload")
    payments = parse_payments(payments_json)
    cheque_info = parse_cheque_info(cheque_info_json)
    share_cheque_info(payments, cheque_info)
    trace.count(files=len(files), payments=len(payments))
    _check_counts(len(files), payments, settings, "files")

    request_id = trace.request_id
    sources = [f.filename or "unnamed" for f in files]
    logger.info("[%s] Match upload request: %d file(s), %d payment(s), model=%s",
                request_id, len(sources), len(payments), settings.gemini_model)

    with trace.stage("load"):
        loaded = [await _read_upload(f, settings, trace, request_id) for f in files]
    payments = await _match(sources, loaded, payments, settings, extractor, trace, background,
                            uploaded=True)
    return MatchResponse(cheque_info=cheque_info, payments=payments)


def parse_payments(payments_json: str) -> list[RentPayment]:
    """The 'payments' form field: a JSON list of payments, same keys as in /cheques/match."""
    try:
        return _PAYMENTS.validate_json(payments_json or "")
    except ValidationError as exc:
        logger.warning("Invalid 'payments' field: %s",
                       [(e.get("loc"), e.get("msg")) for e in exc.errors()])
        if any(e.get("type") == CHEQUE_INFO_IN_PAYMENT for e in exc.errors()):
            raise ChequeInfoInPaymentError() from None
        raise InvalidPaymentsError() from None


def parse_cheque_info(cheque_info_json: Optional[str]) -> Optional[ChequeInfo]:
    """The optional 'cheque_info' form field: a JSON object, same keys as in /cheques/match."""
    text = (cheque_info_json or "").strip()
    if not text or text == "null":
        return None
    try:
        return ChequeInfo.model_validate_json(text)
    except ValidationError as exc:
        logger.warning("Invalid 'cheque_info' field: %s",
                       [(e.get("loc"), e.get("msg")) for e in exc.errors()])
        raise InvalidChequeInfoError() from None


def _check_counts(files: int, payments: list[RentPayment], settings: Settings, what: str) -> None:
    if not payments:
        raise InvalidRequestError("The payment list is empty.")
    if not files:
        raise InvalidRequestError(f"No {what} provided.")
    if files > settings.max_files_per_request:
        raise InvalidRequestError(
            f"Too many files. Send at most {settings.max_files_per_request} {what} per request."
        )


async def _match(
    sources: list[str], loaded: list[Union[UploadedFile, FileResult]], payments: list[RentPayment],
    settings: Settings, extractor: ChequeExtractor, trace: RequestTrace,
    background: Optional[BackgroundTasks] = None, uploaded: bool = False,
) -> list[RentPayment]:
    request_id = trace.request_id
    with trace.stage("extraction"):
        results, uploads = await _extract(loaded, extractor)
    with trace.stage("matching"):
        misses: list[Match] = []
        matches = match_payments(payments, list(zip(sources, results)), misses)
    with trace.stage("cropping"):
        await _crop_matched(matches, sources, results, uploads, settings, request_id, uploaded)

    _log(request_id, trace, results, payments, matches)
    if background is not None:  # runs after the response is sent; never changes it
        job = case_study.prepare(sources, results, uploads, payments, misses, settings, trace)
        if job:
            background.add_task(case_study.save, job, settings)
    return payments


async def _read_upload(
    upload: UploadFile, settings: Settings, trace: RequestTrace, request_id: str
) -> Union[UploadedFile, FileResult]:
    """Read + validate one uploaded file; an invalid file becomes an error FileResult."""
    name = upload.filename or "unnamed"
    start = time.perf_counter()
    try:
        file = await read_and_validate(upload, settings)
    except FileValidationError as exc:
        logger.warning("[%s] Rejected %s: %s", request_id, name, exc.message)
        trace.new_file(source=name, name=name, status="rejected", error=exc.message,
                       load_ms=int((time.perf_counter() - start) * 1000))
        return FileResult(file_name=name, status="error", error=exc.message)
    entry = trace.new_file(source=name, name=file.name, mime_type=file.mime_type,
                           size_kb=round(len(file.data) / 1024, 1),
                           load_ms=int((time.perf_counter() - start) * 1000))
    return dataclasses.replace(file, trace=entry)


async def _load_all(
    sources: list[str], settings: Settings, trace: RequestTrace, request_id: str
) -> list[Union[UploadedFile, FileResult]]:
    # Trace entries are created here, in request order, before files load concurrently
    entries = [trace.new_file(source=redact(s)) for s in sources]
    async with httpx.AsyncClient(timeout=settings.download_timeout_seconds) as client:
        return list(await asyncio.gather(*(
            _load(s, e, client, settings, request_id) for s, e in zip(sources, entries)
        )))


async def _extract(
    loaded: list[Union[UploadedFile, FileResult]], extractor: ChequeExtractor
) -> tuple[list[FileResult], dict[int, UploadedFile]]:
    """One FileResult per source (same order as `sources`), plus the loaded files by index."""
    results: list[Optional[FileResult]] = [r if isinstance(r, FileResult) else None for r in loaded]
    uploads = {i: f for i, f in enumerate(loaded) if isinstance(f, UploadedFile)}
    extracted = await extractor.extract_many(list(uploads.values()))
    for i, result in zip(uploads, extracted):
        results[i] = result
    return results, uploads


async def _crop_matched(
    matches: list[Match], sources: list[str], results: list[FileResult],
    uploads: dict[int, UploadedFile], settings: Settings, request_id: str, uploaded: bool = False,
) -> None:
    """Crop / rotate only the matched cheques and set CroppedOCRFileURL on their payments."""
    wanted: dict[int, set[int]] = {}
    for m in matches:
        wanted.setdefault(m.file_index, set()).add(m.cheque_index)

    await asyncio.gather(*(
        attach_cheque_files(sources[f], uploads[f], results[f], settings, request_id,
                            only=indexes, uploaded=uploaded)
        for f, indexes in wanted.items()
    ))
    for m in matches:
        cropped = results[m.file_index].cheque_files[m.cheque_index]
        m.payment.CroppedOCRFileURL = cropped if cropped != sources[m.file_index] else None


async def _load(
    source: str, entry: dict, client: httpx.AsyncClient, settings: Settings, request_id: str
) -> Union[UploadedFile, FileResult]:
    """Load + validate one file; a failure becomes an error FileResult."""
    start = time.perf_counter()
    try:
        file = await load_file(source, client, settings)
    except AppError as exc:
        logger.warning("[%s] Could not load %s: %s",
                       request_id, redact(source), exc.__cause__ or exc.message)
        entry.update(name=file_name_from_source(source), status="load_error", error=exc.message,
                     error_detail=str(exc.__cause__ or "")[:300] or None,
                     load_ms=int((time.perf_counter() - start) * 1000))
        return FileResult(file_name=file_name_from_source(source), status="error", error=exc.message)
    entry.update(name=file.name, mime_type=file.mime_type, size_kb=round(len(file.data) / 1024, 1),
                 load_ms=int((time.perf_counter() - start) * 1000))
    return dataclasses.replace(file, trace=entry)


def _log(
    request_id: str,
    trace: RequestTrace,
    results: list[FileResult],
    payments: list[RentPayment],
    matches: list[Match],
) -> None:
    matched = sum(bool(p.MatchedWithOcr) for p in payments)
    usage = TokenUsage.total(results)
    cheques = [c for r in results for c in r.cheques]
    matched_at = {id(m.payment): (m.file_index, m.cheque_index) for m in matches}

    for p in payments:
        logger.info("[%s] Payment %s (inst %s, %s, %s, %s): matched=%s file=%s cropped=%s | %s",
                    request_id, p.RentPaymentId, p.Installment, p.ServiceDescription,
                    p.Amount, p.DueDate, p.MatchedWithOcr,
                    redact(p.MatchedOcrFileURL or "-"), redact(p.CroppedOCRFileURL or "-"),
                    p.OcrResponse)
    logger.info("[%s] Match completed: %d/%d payments matched | files ok=%d/%d | tokens=%d | %d ms",
                request_id, matched, len(payments), sum(r.status == "success" for r in results),
                len(results), usage.total_tokens, trace.elapsed_ms())

    trace.count(
        files_ok=sum(r.status == "success" for r in results),
        files_failed=sum(r.status == "error" for r in results),
        cheques_found=len(cheques),
        cheques_processed=sum(1 for p in payments if p.CroppedOCRFileURL),
        payments_matched=matched,
        payments_unmatched=len(payments) - matched,
    )
    trace.set(
        usage=usage.model_dump(),
        issues_summary=dict(Counter(i.code.value for c in cheques for i in c.issues)),
        payments=[{
            "RentPaymentId": p.RentPaymentId,
            "Installment": p.Installment,
            "Amount": p.Amount,
            "DueDate": p.DueDate,
            "checks_sent": [k for k in CHEQUE_INFO_KEYS if p.expected(k)],  # which, never the values
            "matched": p.MatchedWithOcr,
            "matched_cheque": dict(zip(("file", "cheque"), matched_at[id(p)])) if id(p) in matched_at else None,
            "file": redact(p.MatchedOcrFileURL) if p.MatchedOcrFileURL else None,
            "cropped_file": redact(p.CroppedOCRFileURL) if p.CroppedOCRFileURL else None,
            "OcrResponse": p.OcrResponse,
        } for p in payments],
    )
