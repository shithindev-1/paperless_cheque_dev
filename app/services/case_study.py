"""Case study: keep the files behind failed matches, to review why they failed.

After a /cheques/match or /cheques/match/upload response has been sent, every file behind an
unmatched payment, and every file that could not be read, is saved to
<case_study_folder>/<request id>/:

    <name>_ocr[_N].pdf      that cheque cropped / rotated, when the file needs it, else
    <name>.<ext>            the original file (blob: server-side copy, nothing downloaded)
    <name>_case[_N].json    what the OCR read from that cheque and why each payment failed

plus one line per failure in <case_study_folder>/_index/<YYYY-MM-DD>.jsonl (UAE date).

Each failed cheque is saved once: its fingerprint (hash of the file's content + cheque number)
is claimed in <case_study_folder>/_seen/<fingerprint>.json. When the same file fails again in a
later request, nothing is saved; only an index line with "repeat_of": <first request id>.

prepare() runs inside the request and only collects what is already in memory; save() runs
as a background task after the response, at most two at a time. It never changes the
response: any error is only logged.
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

from app.core.config import Settings
from app.core.timezones import now_uae
from app.core.tracing import RequestTrace
from app.schemas import FileResult, RentPayment
from app.services.blob_storage import get_blob_storage
from app.services.cheque_imaging import processed_name
from app.services.file_loader import is_url
from app.services.image_processor import needs_processing, process_cheque_detailed
from app.services.payment_matcher import Match
from app.utils.files import UploadedFile

logger = logging.getLogger(__name__)

_PDF = "application/pdf"
_JSON = "application/json"
_NO_CHEQUES = "No cheques found in this file."
_slots = asyncio.Semaphore(2)  # background saves at once, so live requests keep the CPU
_local_index_lock = asyncio.Lock()


@dataclass
class Case:
    """One failed cheque (cheque_index set) or one unreadable file (cheque_index None)."""
    file_index: int
    cheque_index: Optional[int]
    record: dict  # the <name>_case.json content, without the stored file paths


@dataclass
class CaseStudyJob:
    request_id: str
    endpoint: str
    sources: list[str]
    results: list[FileResult]
    uploads: dict[int, UploadedFile]
    cases: list[Case]
    # Failed payments with no file to point at (e.g. all cheques already used)
    orphans: list[dict] = field(default_factory=list)


def prepare(
    sources: list[str], results: list[FileResult], uploads: dict[int, UploadedFile],
    payments: list[RentPayment], misses: list[Match], settings: Settings, trace: RequestTrace,
) -> Optional[CaseStudyJob]:
    """The cases of this request, or None when nothing failed or the case study is off."""
    if not settings.case_study_enabled or not settings.case_study_folder:
        return None
    request_id, endpoint = trace.request_id, trace.record.get("endpoint")
    cases, orphans = build_cases(sources, results, payments, misses, request_id, endpoint)
    if not cases and not orphans:
        return None
    trace.event("case_study", cases=len(cases), failed_payments=sum(not p.MatchedWithOcr for p in payments))
    return CaseStudyJob(request_id, endpoint, sources, results, uploads, cases, orphans)


def build_cases(
    sources: list[str], results: list[FileResult], payments: list[RentPayment],
    misses: list[Match], request_id: str, endpoint: Optional[str],
) -> tuple[list[Case], list[dict]]:
    """(cases, failed payments that point at no file). No I/O."""
    by_cheque: dict[tuple[int, int], list[dict]] = {}
    for m in misses:
        by_cheque.setdefault((m.file_index, m.cheque_index), []).append(_payment(m.payment))
    referenced = {id(m.payment) for m in misses}
    unreferenced = [_payment(p) for p in payments if not p.MatchedWithOcr and id(p) not in referenced]

    def record(f: int, cheque_index: Optional[int], failed: list[dict], **extra) -> dict:
        result = results[f]
        return {
            "request_id": request_id,
            "created_at": now_uae().isoformat(timespec="seconds"),
            "endpoint": endpoint,
            "file": {"name": _file_name(sources[f], result), "source": _plain(sources[f]),
                     "cheque_index": cheque_index, "cheques_in_file": len(result.cheques)},
            **extra,
            "failed_payments": failed,
        }

    cases = [Case(f, c, record(f, c, failed, ocr_read=_ocr_read(results[f].cheques[c])))
             for (f, c), failed in sorted(by_cheque.items())]
    # Files with nothing to match: the unreferenced payments may well be on them
    bad_files = [f for f, r in enumerate(results) if r.status == "error" or not r.cheques]
    cases += [Case(f, None, record(f, None, unreferenced, error=results[f].error or _NO_CHEQUES))
              for f in bad_files]
    return cases, ([] if bad_files else unreferenced)


async def save(job: CaseStudyJob, settings: Settings) -> None:
    """Background task: save every case. Never raises."""
    start = time.perf_counter()
    try:
        async with _slots:
            store = _store_for(settings, job.request_id)
            if store is None:
                logger.info("[%s] CASE_STUDY skipped: no writable location", job.request_id)
                return
            index: list[dict] = []
            by_file: dict[int, list[Case]] = {}
            for case in job.cases:
                by_file.setdefault(case.file_index, []).append(case)
            for f, cases in by_file.items():
                index += await _save_file(job, f, cases, store, settings)
            index += [_index_line(job, None, None, p, store.folder) for p in job.orphans]
            await store.append_index(index)
        repeats = sum(1 for line in index if line["repeat_of"])
        logger.info("[%s] CASE_STUDY %d failure(s), %d repeat(s) of earlier requests -> %s in %d ms",
                    job.request_id, len(index), repeats, store.folder,
                    int((time.perf_counter() - start) * 1000))
    except Exception:
        logger.exception("[%s] CASE_STUDY failed; the response was not affected", job.request_id)


async def _save_file(
    job: CaseStudyJob, f: int, cases: list[Case], store: "_Store", settings: Settings
) -> list[dict]:
    """The failed cheques of one file not saved before: the cropped / rotated cheque when the
    file needs it, else the original file (saved once, however many of its cheques failed)."""
    result, file = job.results[f], job.uploads.get(f)
    name = _stored_name(job, f)
    total = len(result.cheques)

    index: list[dict] = []
    new: list[Case] = []
    for case in cases:  # a cheque already saved by an earlier request is only indexed
        first = await store.claim(_fingerprint(job, f, case.cheque_index), _marker(job, name, case))
        if first is None:
            new.append(case)
        else:
            index += [_index_line(job, case, case.record["file"]["name"], p, None, first)
                      for p in case.record["failed_payments"] or [None]]
    cases = new
    if not cases:
        return index

    cropped = {case.cheque_index: await _save_crop(job, f, case.cheque_index, name, file, store,
                                                   settings)
               for case in cases}
    original = None
    if any(c is None for c in cropped.values()):
        original = await _save_original(job.sources[f], name, file, store, job.request_id)

    stem = PurePosixPath(name).stem
    for case in cases:
        c = case.cheque_index
        record = dict(case.record)
        record["file"] = {**record["file"], "original": None if cropped[c] else original,
                          "cropped": cropped[c]}
        number = f"_{c + 1}" if c is not None and total > 1 else ""
        case_file = await store.put(f"{stem}_case{number}.json",
                                    json.dumps(record, ensure_ascii=False, indent=2).encode(), _JSON)
        index += [_index_line(job, case, record["file"]["name"], p, case_file)
                  for p in record["failed_payments"] or [None]]
    return index


async def _save_crop(
    job: CaseStudyJob, f: int, c: Optional[int], name: str, file: Optional[UploadedFile],
    store: "_Store", settings: Settings,
) -> Optional[str]:
    """The cheque cropped / rotated, when the file needs it (same rule as matched cheques)."""
    cheques = job.results[f].cheques
    if c is None or file is None or not settings.image_processing_enabled \
            or not needs_processing(cheques):
        return None
    try:
        pdf, _ = await asyncio.to_thread(
            process_cheque_detailed, file.data, file.mime_type, cheques[c],
            padding_percent=settings.crop_padding_percent, dpi=settings.pdf_render_dpi,
            quality=settings.processed_jpeg_quality)
        return await store.put(
            processed_name(name, settings.processed_file_suffix, c + 1, len(cheques)), pdf, _PDF)
    except Exception as exc:  # no crop: the original file is kept instead
        logger.warning("[%s] CASE_STUDY could not crop cheque %d of %s: %s",
                       job.request_id, c, name, exc)
        return None


async def _save_original(
    source: str, name: str, file: Optional[UploadedFile], store: "_Store", request_id: str
) -> Optional[str]:
    try:
        if is_url(source) and store.storage:
            blob = store.storage.blob_name(source)
            if blob:
                try:
                    return await store.copy(blob, name)
                except Exception as exc:  # e.g. copy not allowed: upload the bytes we have
                    if file is None:
                        raise
                    logger.info("[%s] CASE_STUDY copy failed (%s); uploading instead", request_id, exc)
        if file is not None:
            return await store.put(name, file.data, file.mime_type)
    except Exception as exc:
        logger.warning("[%s] CASE_STUDY could not save original %s: %s", request_id, name, exc)
    return None


def _fingerprint(job: CaseStudyJob, f: int, c: Optional[int]) -> str:
    """Same file content (whatever its name) + same cheque = same fingerprint. A file that
    could not be loaded has no content: its source and error are used instead."""
    file = job.uploads.get(f)
    content = file.data if file else f"{_plain(job.sources[f])}|{job.results[f].error}".encode()
    return f"{hashlib.sha256(content).hexdigest()}_{'file' if c is None else c}"


def _marker(job: CaseStudyJob, name: str, case: Case) -> bytes:
    return json.dumps({"first_request_id": job.request_id, "file": name,
                       "cheque_index": case.cheque_index,
                       "saved_at": now_uae().isoformat(timespec="seconds")}).encode()


def _index_line(job: CaseStudyJob, case: Optional[Case], file_name: Optional[str],
                payment: Optional[dict], case_file: Optional[str],
                repeat_of: Optional[str] = None) -> dict:
    return {
        "at": now_uae().isoformat(timespec="seconds"),
        "request_id": job.request_id,
        "endpoint": job.endpoint,
        "file": file_name,
        "cheque_index": case.cheque_index if case else None,
        "RentPaymentId": payment.get("RentPaymentId") if payment else None,
        "OcrResponse": payment.get("OcrResponse") if payment else case.record.get("error"),
        "case_file": case_file,
        "repeat_of": repeat_of,
    }


# ── content ──

def _payment(p: RentPayment) -> dict:
    return {
        "RentPaymentId": p.RentPaymentId,
        "Installment": p.Installment,
        "Amount": p.Amount,
        "DueDate": p.DueDate,
        "cheque_info": p.cheque_info.model_dump() if p.cheque_info else None,
        "OcrResponse": p.OcrResponse,
    }


def _ocr_read(cheque) -> dict:
    orientation = getattr(cheque, "orientation", None)
    return {
        "page": getattr(cheque, "page", None),
        "date": cheque.cheque_date,
        "amount": cheque.cheque_amount,
        "amount_in_words": cheque.amount_in_words,
        "payee_name": cheque.payee_name,
        "holder_name": cheque.holder_name,
        "holder_name_ar": getattr(cheque, "holder_name_ar", None),
        "iban": cheque.iban,
        "unclear_fields": [f.value for f in cheque.unclear_fields],
        "issues": [{"code": i.code.value, "detail": i.detail} for i in cheque.issues],
        "orientation": orientation.value if orientation else None,
        "error": cheque.error,
    }


def _plain(source: str) -> str:
    return source.split("?")[0]  # never keep a SAS token


def _file_name(source: str, result: FileResult) -> str:
    return PurePosixPath(_plain(source).replace("\\", "/")).name or result.file_name or "file"


def _stored_name(job: CaseStudyJob, f: int) -> str:
    """File name inside the request folder; a repeated name gets its position: 'a_2.jpg'."""
    name = _file_name(job.sources[f], job.results[f])
    if [_file_name(s, r) for s, r in zip(job.sources, job.results)].count(name) == 1:
        return name
    path = PurePosixPath(name)
    return f"{path.stem}_{f + 1}{path.suffix}"


# ── storage ──

class _Store:
    """<case_study_folder>/<request id>/ in blob storage, or under LOCAL_FILES_DIR locally."""

    def __init__(self, settings: Settings, request_id: str, storage=None) -> None:
        self.root = settings.case_study_folder.strip("/\\").replace("\\", "/")
        self.folder = f"{self.root}/{request_id}"
        self.storage = storage
        self._base = None if storage else Path(settings.local_files_dir).resolve()

    async def put(self, name: str, data: bytes, content_type: str) -> str:
        path = f"{self.folder}/{name}"
        if self.storage:
            return await self.storage.upload(path, data, content_type)
        target = self._local(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, data)
        return path

    async def claim(self, fingerprint: str, marker: bytes) -> Optional[str]:
        """Record the fingerprint; None if new, else the request id that saved it first."""
        path = f"{self.root}/_seen/{fingerprint}.json"
        if self.storage:
            if await self.storage.create_if_missing(path, marker, _JSON):
                return None
            existing, _ = await self.storage.download(path)
        else:
            target = self._local(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                await asyncio.to_thread(_create_new, target, marker)
                return None
            except FileExistsError:
                existing = await asyncio.to_thread(target.read_bytes)
        try:
            return json.loads(existing).get("first_request_id") or "unknown"
        except ValueError:
            return "unknown"

    async def copy(self, source_blob: str, name: str) -> str:
        return await self.storage.copy(source_blob, f"{self.folder}/{name}")

    async def append_index(self, lines: list[dict]) -> None:
        if not lines:
            return
        path = f"{self.root}/_index/{now_uae():%Y-%m-%d}.jsonl"
        data = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines).encode()
        if self.storage:
            await self.storage.append(path, data, "application/x-ndjson")
            return
        target = self._local(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        async with _local_index_lock:
            await asyncio.to_thread(_append_bytes, target, data)

    def _local(self, path: str) -> Path:
        target = (self._base / path).resolve()
        if not target.is_relative_to(self._base):
            raise ValueError("Case study path escapes LOCAL_FILES_DIR")
        return target


def _create_new(target: Path, data: bytes) -> None:
    with target.open("xb") as fh:  # fails if it exists: only one request claims a fingerprint
        fh.write(data)


def _append_bytes(target: Path, data: bytes) -> None:
    with target.open("ab") as fh:
        fh.write(data)


def _store_for(settings: Settings, request_id: str) -> Optional[_Store]:
    if settings.local_files_enabled:
        return _Store(settings, request_id)
    storage = get_blob_storage(settings)
    return _Store(settings, request_id, storage) if storage else None
