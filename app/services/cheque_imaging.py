"""Crop / straighten extracted cheques and save them next to the original file.

Only the cheques asked for (the matched ones) are processed, and only when the file
holds several cheques or the OCR model reports the cheque needs cropping or rotation.
Otherwise (or on any failure) the cheque keeps the original file URL. Results go into
FileResult.cheque_files.
"""

import asyncio
import logging
import time
from pathlib import Path, PurePosixPath
from typing import Optional

from app.core.config import Settings
from app.core.tracing import current_trace
from app.schemas import FileResult
from app.services.blob_storage import get_blob_storage
from app.services.file_loader import is_url, redact
from app.services.image_processor import needs_processing, process_cheque_detailed
from app.utils.files import UploadedFile

logger = logging.getLogger(__name__)

_PDF = "application/pdf"


async def attach_cheque_files(
    source: str, file: UploadedFile, result: FileResult, settings: Settings, request_id: str,
    only: Optional[set[int]] = None,
) -> None:
    """Fill result.cheque_files with one URL / path per cheque.

    `only` limits processing to these cheque indexes (0-based); the rest keep the original.
    """
    cheques = result.cheques
    result.cheque_files = [source] * len(cheques)
    if not cheques or not settings.image_processing_enabled or not needs_processing(cheques):
        return
    if only is not None and not only:
        return

    save = _saver(source, settings, len(cheques), request_id)
    if save is None:
        logger.info("[%s] %s needs crop/rotation but has no writable location; keeping original",
                    request_id, redact(source))
        return

    for i, cheque in enumerate(cheques, start=1):
        if only is not None and i - 1 not in only:
            continue
        step = {"file": file.trace.get("index"), "cheque": i, "page": cheque.page,
                "needs_crop": cheque.needs_crop}
        start = time.perf_counter()
        try:
            image, info = await asyncio.to_thread(
                process_cheque_detailed, file.data, file.mime_type, cheque,
                padding_percent=settings.crop_padding_percent,
                dpi=settings.pdf_render_dpi,
                quality=settings.processed_jpeg_quality,
            )
            step.update(info, process_ms=_ms(start))
            saved = time.perf_counter()
            result.cheque_files[i - 1] = await save(i, image)
            step.update(save_ms=_ms(saved), size_kb=round(len(image) / 1024, 1),
                        output=redact(result.cheque_files[i - 1]), status="ok")
            logger.info("[%s] Processed cheque %d of %s (page=%s, turn=%s, tilt=%s, crop=%s) -> %s",
                        request_id, i, redact(source), cheque.page, info["quarter_turn"],
                        info["tilt_applied"], cheque.needs_crop, redact(result.cheque_files[i - 1]))
        except Exception as exc:  # keep the original file for this cheque
            step.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:200]}",
                        process_ms=_ms(start))
            logger.exception("[%s] Could not process cheque %d of %s; keeping original",
                             request_id, i, redact(source))
        trace = current_trace()
        if trace:
            trace.cheque_processed(**step)


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def processed_name(
    name: str, suffix: str, index: int, total: int,
    folder: Optional[str] = None, request_id: Optional[str] = None,
) -> str:
    """Name of a processed cheque (always .pdf).

    No folder:   'dir/abc.jpeg' -> 'dir/abc_ocr.pdf' (several cheques: 'dir/abc_ocr_1.pdf', ...)
    With folder: 'dir/abc.jpeg' -> '<folder>/<request_id>/abc_ocr.pdf'
    """
    path = PurePosixPath(name)
    number = f"_{index}" if total > 1 else ""
    file_name = f"{path.stem}{suffix}{number}.pdf"
    if folder:
        return str(PurePosixPath(folder.strip("/")) / request_id / file_name)
    return str(path.with_name(file_name))


def _saver(source: str, settings: Settings, total: int, request_id: str):
    """Async `save(index, pdf_bytes) -> url_or_path` for this source, or None if not writable."""
    def name_for(original: str, index: int) -> str:
        return processed_name(original, settings.processed_file_suffix, index, total,
                              settings.processed_folder, request_id)

    if is_url(source):
        storage = get_blob_storage(settings)
        blob_name: Optional[str] = storage.blob_name(source) if storage else None
        if not blob_name:
            return None

        async def save_blob(index: int, data: bytes) -> str:
            return await storage.upload(name_for(blob_name, index), data, _PDF)
        return save_blob

    if not settings.local_files_enabled:
        return None
    base = Path(settings.local_files_dir).resolve()

    async def save_local(index: int, data: bytes) -> str:
        name = name_for(Path(source).as_posix(), index)
        target = (base / name).resolve()
        if not target.is_relative_to(base):
            raise ValueError("Processed file path escapes LOCAL_FILES_DIR")
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, data)
        return name
    return save_local
