"""Azure Blob Storage access for cheque files under AZURE_BLOB_BASE_URL.

The connection string comes from the environment only. Only URLs inside the
configured container (the base URL) are read or written with it.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, unquote, urlparse

from azure.core.exceptions import AzureError
from azure.storage.blob import BlobSasPermissions, BlobServiceClient, ContentSettings, generate_blob_sas

from app.core.config import Settings
from app.core.exceptions import FileDownloadError, FileValidationError


class BlobStorage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        base = urlparse(settings.azure_blob_base_url.rstrip("/") + "/")
        self._host = (base.hostname or "").lower()
        parts = unquote(base.path).strip("/").split("/", 1)
        self.container = parts[0]
        self._prefix = parts[1] + "/" if len(parts) > 1 and parts[1] else ""
        self._service = BlobServiceClient.from_connection_string(
            settings.azure_storage_connection_string.get_secret_value()
        )

    # ── URL <-> blob name ──

    def blob_name(self, url: str) -> Optional[str]:
        """Blob name if `url` is inside the configured container (and prefix), else None."""
        parsed = urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != self._host:
            return None
        path = unquote(parsed.path).lstrip("/")
        container, _, name = path.partition("/")
        if container != self.container or not name or not name.startswith(self._prefix):
            return None
        return name

    def url_for(self, blob_name: str) -> str:
        """Public URL of a blob, with a read-only SAS token if AZURE_SAS_EXPIRY_HOURS > 0."""
        url = f"https://{self._host}/{self.container}/{quote(blob_name)}"
        hours = self.settings.azure_sas_expiry_hours
        if not hours:
            return url
        token = generate_blob_sas(
            account_name=self._service.account_name,
            container_name=self.container,
            blob_name=blob_name,
            account_key=self._service.credential.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=hours),
        )
        return f"{url}?{token}"

    # ── I/O (sync SDK run in a worker thread) ──

    async def download(self, blob_name: str) -> tuple[bytes, Optional[str]]:
        return await asyncio.to_thread(self._download, blob_name)

    async def upload(self, blob_name: str, data: bytes, content_type: str) -> str:
        await asyncio.to_thread(self._upload, blob_name, data, content_type)
        return self.url_for(blob_name)

    def _download(self, blob_name: str) -> tuple[bytes, Optional[str]]:
        client = self._service.get_blob_client(self.container, blob_name)
        try:
            props = client.get_blob_properties()
            if props.size > self.settings.max_file_size_bytes:
                raise FileValidationError(
                    f"The file exceeds the {self.settings.max_file_size_mb} MB limit."
                )
            data = client.download_blob().readall()
        except AzureError as exc:
            raise FileDownloadError() from exc
        return data, props.content_settings.content_type

    def _upload(self, blob_name: str, data: bytes, content_type: str) -> None:
        client = self._service.get_blob_client(self.container, blob_name)
        client.upload_blob(data, overwrite=True,
                           content_settings=ContentSettings(content_type=content_type))


_clients: dict[int, BlobStorage] = {}


def get_blob_storage(settings: Settings) -> Optional[BlobStorage]:
    """Shared client per Settings instance, or None when blob storage is not configured."""
    if not settings.blob_storage_enabled:
        return None
    if id(settings) not in _clients:
        _clients[id(settings)] = BlobStorage(settings)
    return _clients[id(settings)]
