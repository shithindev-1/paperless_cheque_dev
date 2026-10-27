import httpx
import pytest

from app.core.exceptions import FileValidationError
from app.services.file_loader import file_name_from_source, load_file, redact
from app.utils.files import sniff_mime_type

PDF_BYTES = b"%PDF-1.4\n%test\n"


async def _load(source, settings):
    async with httpx.AsyncClient() as client:
        return await load_file(source, client, settings)


def test_sniff_mime_type():
    assert sniff_mime_type(PDF_BYTES) == "application/pdf"
    assert sniff_mime_type(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert sniff_mime_type(b"hello") is None


def test_file_name_and_redact():
    url = "https://acc.blob.core.windows.net/c/cheque%201.pdf?sv=secret"
    assert file_name_from_source(url) == "cheque 1.pdf"
    assert redact(url) == "https://acc.blob.core.windows.net/c/cheque%201.pdf"


async def test_local_file_is_loaded(settings, tmp_path):
    (tmp_path / "cheque.pdf").write_bytes(PDF_BYTES)
    loaded = await _load("cheque.pdf", settings)
    assert loaded.name == "cheque.pdf"
    assert loaded.mime_type == "application/pdf"


async def test_local_path_outside_folder_is_rejected(settings):
    with pytest.raises(FileValidationError):
        await _load("../outside.pdf", settings)


async def test_local_files_disabled_outside_local_env(settings, tmp_path):
    (tmp_path / "cheque.pdf").write_bytes(PDF_BYTES)
    uat = settings.model_copy(update={"app_env": "uat"})
    with pytest.raises(FileValidationError, match="Only file URLs"):
        await _load("cheque.pdf", uat)


@pytest.mark.parametrize("url", [
    "http://acc.blob.core.windows.net/c/a.pdf",   # not https
    "https://evil.example.com/a.pdf",              # host not allowed
])
async def test_disallowed_urls_are_rejected(settings, url):
    with pytest.raises(FileValidationError, match="not allowed"):
        await _load(url, settings)


async def test_unsupported_file_type(settings, tmp_path):
    (tmp_path / "note.txt").write_bytes(b"hello")
    with pytest.raises(FileValidationError, match="Unsupported"):
        await _load("note.txt", settings)
