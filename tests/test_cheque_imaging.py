import cv2
import numpy as np
import pytest

from app.core.config import Settings
from app.schemas import ChequeOcr, FileResult
from app.services.blob_storage import BlobStorage
from app.services.cheque_imaging import attach_cheque_files, processed_name
from app.utils.files import UploadedFile

FAKE_CONN = ("DefaultEndpointsProtocol=https;AccountName=acc;"
             "AccountKey=dGVzdGtleXRlc3RrZXk=;EndpointSuffix=core.windows.net")
BASE = "https://acc.blob.core.windows.net/files-uat/"


def _jpeg() -> bytes:
    return cv2.imencode(".jpg", np.full((400, 800, 3), 255, np.uint8))[1].tobytes()


def _result(*cheques) -> FileResult:
    return FileResult(file_name="c.jpeg", status="success", cheques=list(cheques))


def test_processed_name():
    assert processed_name("App/OCR/Temp/abc.jpeg", "_processed", 1) == "App/OCR/Temp/abc_processed_1.jpeg"
    assert processed_name("cheque_1.pdf", "_processed", 2) == "cheque_1_processed_2.jpeg"


async def test_no_processing_keeps_original(settings):
    result = _result(ChequeOcr(rotation_degrees=0, needs_crop=False))
    await attach_cheque_files("c.jpeg", UploadedFile("c.jpeg", _jpeg(), "image/jpeg"),
                              result, settings, "t")
    assert result.cheque_files == ["c.jpeg"]


async def test_local_processing_writes_next_to_original(settings, tmp_path):
    result = _result(
        ChequeOcr(page=1, bounding_box=[0, 0, 500, 1000], rotation_degrees=0, needs_crop=True),
        ChequeOcr(page=1, bounding_box=[500, 0, 1000, 1000], rotation_degrees=180, needs_crop=True),
    )
    await attach_cheque_files("c.jpeg", UploadedFile("c.jpeg", _jpeg(), "image/jpeg"),
                              result, settings, "t")
    assert result.cheque_files == ["c_processed_1.jpeg", "c_processed_2.jpeg"]
    assert (tmp_path / "c_processed_1.jpeg").stat().st_size > 0


async def test_failed_cheque_keeps_original(settings):
    result = _result(ChequeOcr(bounding_box=[600, 0, 100, 1000], needs_crop=True))  # invalid box
    await attach_cheque_files("c.jpeg", UploadedFile("c.jpeg", _jpeg(), "image/jpeg"),
                              result, settings, "t")
    assert result.cheque_files == ["c.jpeg"]


async def test_external_url_is_not_written(settings):
    url = "https://other.blob.core.windows.net/x/c.jpeg?sig=s"
    result = _result(ChequeOcr(needs_crop=True), ChequeOcr(needs_crop=True))
    await attach_cheque_files(url, UploadedFile("c.jpeg", _jpeg(), "image/jpeg"),
                              result, settings, "t")
    assert result.cheque_files == [url, url]


@pytest.fixture
def blob_settings() -> Settings:
    return Settings(_env_file=None, gemini_api_key="k", gemini_model="m",
                    azure_storage_connection_string=FAKE_CONN, azure_blob_base_url=BASE)


def test_blob_name_only_inside_container(blob_settings):
    storage = BlobStorage(blob_settings)
    assert storage.blob_name(BASE + "ApplicationFiles/OCR/Temp/a1.jpeg") == "ApplicationFiles/OCR/Temp/a1.jpeg"
    assert storage.blob_name(BASE + "a%20b.jpeg?sv=1") == "a b.jpeg"
    assert storage.blob_name("https://acc.blob.core.windows.net/other/a.jpeg") is None
    assert storage.blob_name("https://evil.com/files-uat/a.jpeg") is None
    assert storage.blob_name("http://acc.blob.core.windows.net/files-uat/a.jpeg") is None


def test_url_for_plain_and_sas(blob_settings):
    storage = BlobStorage(blob_settings)
    assert storage.url_for("OCR/Temp/a b.jpeg") == BASE + "OCR/Temp/a%20b.jpeg"
    blob_settings.azure_sas_expiry_hours = 24
    url = storage.url_for("OCR/Temp/a.jpeg")
    assert url.startswith(BASE + "OCR/Temp/a.jpeg?") and "sig=" in url and "sp=r" in url
