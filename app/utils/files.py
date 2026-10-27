"""File type detection and validation (no I/O)."""

import mimetypes
from dataclasses import dataclass
from typing import Optional

from fastapi import UploadFile

from app.core.config import Settings
from app.core.exceptions import FileValidationError

_MAGIC_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
_GENERIC_TYPES = {"", "application/octet-stream", "binary/octet-stream"}


@dataclass(frozen=True)
class UploadedFile:
    name: str
    data: bytes
    mime_type: str


def sniff_mime_type(data: bytes) -> Optional[str]:
    """Detect the file type from its content (magic bytes)."""
    for signature, mime in _MAGIC_SIGNATURES:
        if data.startswith(signature):
            return mime
    if b"%PDF-" in data[:1024]:
        return "application/pdf"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        return "image/heic"
    return None


def resolve_mime_type(content_type: Optional[str], filename: str, data: bytes) -> str:
    """File content first, then the declared content type, then the file extension."""
    sniffed = sniff_mime_type(data)
    if sniffed:
        return sniffed
    content_type = (content_type or "").split(";")[0].strip().lower()
    if content_type not in _GENERIC_TYPES:
        return content_type
    guessed, _ = mimetypes.guess_type(filename)
    return (guessed or "application/octet-stream").lower()


def validate_file(
    name: str, data: bytes, content_type: Optional[str], settings: Settings
) -> UploadedFile:
    """Validate file bytes. Raises FileValidationError if invalid."""
    if not data:
        raise FileValidationError("The file is empty.")
    if len(data) > settings.max_file_size_bytes:
        raise FileValidationError(f"The file exceeds the {settings.max_file_size_mb} MB limit.")

    mime_type = resolve_mime_type(content_type, name, data)
    if mime_type not in settings.allowed_mime_types:
        raise FileValidationError("Unsupported file type. Upload a JPG, PNG, WEBP, HEIC or PDF.")

    return UploadedFile(name=name, data=data, mime_type=mime_type)


async def read_and_validate(upload: UploadFile, settings: Settings) -> UploadedFile:
    """Read an upload and validate it. Raises FileValidationError if invalid."""
    data = await upload.read()
    return validate_file(upload.filename or "unnamed", data, upload.content_type, settings)
