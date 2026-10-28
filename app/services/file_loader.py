"""Load cheque files from Azure URLs (or, for local testing, from LOCAL_FILES_DIR).

URLs inside AZURE_BLOB_BASE_URL are read with the storage connection string, so they
need no SAS token. Other allowed https URLs are downloaded directly.
"""

from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

import httpx

from app.core.config import Settings
from app.core.exceptions import FileDownloadError, FileValidationError
from app.services.blob_storage import get_blob_storage
from app.utils.files import UploadedFile, validate_file


def is_url(source: str) -> bool:
    return source.lower().startswith(("http://", "https://"))


def file_name_from_source(source: str) -> str:
    if is_url(source):
        return unquote(PurePosixPath(urlparse(source).path).name) or "unnamed"
    return Path(source).name or "unnamed"


def redact(source: str) -> str:
    """Drop the query string (e.g. Azure SAS token) before logging a URL."""
    return source.split("?")[0]


async def load_file(source: str, client: httpx.AsyncClient, settings: Settings) -> UploadedFile:
    """Load and validate one file. Raises FileValidationError / FileDownloadError."""
    if is_url(source):
        storage = get_blob_storage(settings)
        blob_name = storage.blob_name(source) if storage else None
        if blob_name:
            data, content_type = await storage.download(blob_name)
            return validate_file(file_name_from_source(source), data, content_type, settings)
        return await _download(source, client, settings)
    return _read_local(source, settings)


def _host_allowed(host: str, allowed: list[str]) -> bool:
    """Empty list = any host. Entries starting with '.' match subdomains."""
    if not allowed:
        return True
    host = host.lower()
    return any(
        host.endswith(entry.lower()) if entry.startswith(".") else host == entry.lower()
        for entry in allowed
    )


async def _download(url: str, client: httpx.AsyncClient, settings: Settings) -> UploadedFile:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not _host_allowed(parsed.hostname or "", settings.allowed_file_hosts):
        raise FileValidationError("File URL is not allowed.")

    chunks: list[bytes] = []
    size = 0
    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > settings.max_file_size_bytes:
                    raise FileValidationError(
                        f"The file exceeds the {settings.max_file_size_mb} MB limit."
                    )
                chunks.append(chunk)
            content_type = response.headers.get("content-type")
    except httpx.HTTPError as exc:
        raise FileDownloadError() from exc

    return validate_file(file_name_from_source(url), b"".join(chunks), content_type, settings)


def _read_local(path: str, settings: Settings) -> UploadedFile:
    """Local testing only. The file must be inside LOCAL_FILES_DIR."""
    if not settings.local_files_enabled:
        raise FileValidationError("Only file URLs are accepted.")
    base = Path(settings.local_files_dir).resolve()
    file = (base / path).resolve()  # absolute paths replace `base`, relative ones join it
    if not file.is_relative_to(base):
        raise FileValidationError("File path is not allowed.")
    if not file.is_file():
        raise FileValidationError("File not found.")
    if file.stat().st_size > settings.max_file_size_bytes:
        raise FileValidationError(f"The file exceeds the {settings.max_file_size_mb} MB limit.")
    return validate_file(file.name, file.read_bytes(), None, settings)
