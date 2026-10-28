"""Crop / straighten extracted cheques and save them next to the original file.

Only the cheques asked for (the matched ones) are processed, and only when the file
holds several cheques or the OCR model reports the cheque needs cropping or rotation.
Otherwise (or on any failure) the cheque keeps the original file URL. Results go into
FileResult.cheque_files.
"""

import asyncio
import logging
from pathlib import Path, PurePosixPath
from typing import Optional

from app.core.config import Settings
from app.schemas import FileResult
from app.services.blob_storage import get_blob_storage
from app.services.file_loader import is_url, redact
from app.services.image_processor import needs_processing, process_cheque
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

    save = _saver(source, settings, len(cheques))
    if save is None:
        logger.info("[%s] %s needs crop/rotation but has no writable location; keeping original",
                    request_id, redact(source))
        return

    for i, cheque in enumerate(cheques, start=1):
        if only is not None and i - 1 not in only:
            continue
        try:
            image = await asyncio.to_thread(
                process_cheque, file.data, file.mime_type, cheque,
                padding_percent=settings.crop_padding_percent,
                dpi=settings.pdf_render_dpi,
                quality=settings.processed_jpeg_quality,
            )
            result.cheque_files[i - 1] = await save(i, image)
            logger.info("[%s] Processed cheque %d of %s (page=%s, rotation=%s, crop=%s) -> %s",
                        request_id, i, redact(source), cheque.page, cheque.rotation_degrees,
                        cheque.needs_crop, redact(result.cheque_files[i - 1]))
        except Exception:  # keep the original file for this cheque
            logger.exception("[%s] Could not process cheque %d of %s; keeping original",
                             request_id, i, redact(source))


def processed_name(name: str, suffix: str, index: int, total: int) -> str:
    """'dir/abc.jpeg' -> 'dir/abc_ocr.pdf'; several cheques -> 'dir/abc_ocr_1.pdf', ... (always .pdf)."""
    path = PurePosixPath(name)
    number = f"_{index}" if total > 1 else ""
    return str(path.with_name(f"{path.stem}{suffix}{number}.pdf"))


def _saver(source: str, settings: Settings, total: int):
    """Async `save(index, pdf_bytes) -> url_or_path` for this source, or None if not writable."""
    suffix = settings.processed_file_suffix
    if is_url(source):
        storage = get_blob_storage(settings)
        blob_name: Optional[str] = storage.blob_name(source) if storage else None
        if not blob_name:
            return None

        async def save_blob(index: int, data: bytes) -> str:
            return await storage.upload(processed_name(blob_name, suffix, index, total), data, _PDF)
        return save_blob

    if not settings.local_files_enabled:
        return None
    base = Path(settings.local_files_dir).resolve()

    async def save_local(index: int, data: bytes) -> str:
        name = processed_name(Path(source).as_posix(), suffix, index, total)
        target = (base / name).resolve()
        if not target.is_relative_to(base):
            raise ValueError("Processed file path escapes LOCAL_FILES_DIR")
        await asyncio.to_thread(target.write_bytes, data)
        return name
    return save_local
