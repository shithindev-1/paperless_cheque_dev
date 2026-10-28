"""Crop / straighten extracted cheques and save them next to the original file.

A processed image is created only when a file holds several cheques, or the OCR
model reports the cheque needs cropping or rotation. Otherwise (or on any failure)
the cheque keeps the original file URL. Results go into FileResult.cheque_files.
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

_JPEG = "image/jpeg"


async def attach_cheque_files(
    source: str, file: UploadedFile, result: FileResult, settings: Settings, request_id: str
) -> None:
    """Fill result.cheque_files with one URL / path per cheque."""
    cheques = result.cheques
    result.cheque_files = [source] * len(cheques)
    if not cheques or not settings.image_processing_enabled or not needs_processing(cheques):
        return

    save = _saver(source, settings)
    if save is None:
        logger.info("[%s] %s needs crop/rotation but has no writable location; keeping original",
                    request_id, redact(source))
        return

    for i, cheque in enumerate(cheques, start=1):
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


def processed_name(name: str, suffix: str, index: int) -> str:
    """'dir/abc.jpeg' -> 'dir/abc_processed_1.jpeg' (always .jpeg)."""
    path = PurePosixPath(name)
    return str(path.with_name(f"{path.stem}{suffix}_{index}.jpeg"))


def _saver(source: str, settings: Settings):
    """Async `save(index, jpeg_bytes) -> url_or_path` for this source, or None if not writable."""
    if is_url(source):
        storage = get_blob_storage(settings)
        blob_name: Optional[str] = storage.blob_name(source) if storage else None
        if not blob_name:
            return None

        async def save_blob(index: int, data: bytes) -> str:
            return await storage.upload(
                processed_name(blob_name, settings.processed_file_suffix, index), data, _JPEG
            )
        return save_blob

    if not settings.local_files_enabled:
        return None
    base = Path(settings.local_files_dir).resolve()

    async def save_local(index: int, data: bytes) -> str:
        name = processed_name(Path(source).as_posix(), settings.processed_file_suffix, index)
        target = (base / name).resolve()
        if not target.is_relative_to(base):
            raise ValueError("Processed file path escapes LOCAL_FILES_DIR")
        await asyncio.to_thread(target.write_bytes, data)
        return name
    return save_local
