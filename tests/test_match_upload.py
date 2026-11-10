import json

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_cheque_extractor
from app.api.errors import register_exception_handlers
from app.api.v1.endpoints import matching
from app.core.config import get_settings
from app.core.exceptions import ChequeInfoInPaymentError, InvalidChequeInfoError, InvalidPaymentsError
from app.schemas import ChequeOcr, FileResult
from app.services.matching_service import parse_cheque_info, parse_payments

WORDS = "Nine Thousand Three Hundred Thirty Three Only"


def _jpeg() -> bytes:
    return cv2.imencode(".jpg", np.full((400, 800, 3), 255, np.uint8))[1].tobytes()


class FakeExtractor:
    """Returns fixed cheques per uploaded file name instead of calling the OCR model."""

    def __init__(self, cheques: dict[str, list[dict]]):
        self.cheques = cheques

    async def extract_many(self, files):
        return [FileResult(file_name=f.name, status="success",
                           cheques=[ChequeOcr(**c) for c in self.cheques[f.name]]) for f in files]


@pytest.fixture
def client(settings):
    settings.processed_folder = "processed"
    extractor = FakeExtractor({
        "a.jpg": [{"cheque_date": "01-01-2026", "cheque_amount": "9333", "amount_in_words": WORDS,
                   "payee_name": "Omar Ali", "page": "1", "bounding_box": [100, 100, 900, 900],
                   "text_top": "up", "needs_crop": True}],
        "b.jpg": [{"cheque_date": "01-03-2026", "cheque_amount": "9333", "amount_in_words": WORDS,
                   "payee_name": "Omar Ali"}],
    })
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(matching.router)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_cheque_extractor] = lambda: extractor
    return TestClient(app)


def _post(client, payments, files=("a.jpg", "b.jpg"), data=None, mime="image/jpeg", cheque_info=None):
    form = {"payments": payments if isinstance(payments, str) else json.dumps(payments)}
    if cheque_info is not None:
        form["cheque_info"] = json.dumps(cheque_info)
    return client.post(
        "/cheques/match/upload",
        files=[("files", (name, data or _jpeg(), mime)) for name in files],
        data=form,
    )


def test_uploaded_files_are_matched_and_verified(client, tmp_path):
    response = _post(client, [
        {"RentPaymentId": 1, "Amount": 9333, "DueDate": "2026-01-01"},
        {"RentPaymentId": 2, "Amount": 9333, "DueDate": "2026-05-01"},
        {"RentPaymentId": 3, "Amount": 9333, "DueDate": "2026-03-01"},
    ], cheque_info={"ChequepayeeName": "OMAR ALI"})
    assert response.status_code == 200
    first, second, third = response.json()["payments"]

    assert first["MatchedWithOcr"] is True and first["OcrResponse"] == "Matched"
    assert first["MatchedOcrFileURL"] == "a.jpg"
    cropped = first["CroppedOCRFileURL"]
    assert cropped.startswith("processed/") and cropped.endswith("/a_ocr.pdf")
    assert (tmp_path / cropped).read_bytes().startswith(b"%PDF")

    assert second["MatchedWithOcr"] is False
    assert second["MatchedOcrFileURL"] == "b.jpg"
    assert second["OcrResponse"] == "Date doesn't match. Cheque date is 01-03-2026."

    # Payee is verified too (case ignored)
    assert third["MatchedWithOcr"] is True and third["MatchedOcrFileURL"] == "b.jpg"


def test_invalid_file_is_reported_per_file(client):
    response = _post(client, [{"Amount": 9333, "DueDate": "2026-01-01"}], files=["notes.txt"],
                     data=b"not a cheque", mime="text/plain")
    assert response.status_code == 200
    assert response.json()["payments"][0]["OcrResponse"] == "None of the cheque files could be processed."


@pytest.mark.parametrize("payments", ["not json", "[]x", '[{"Amount": 1, "DueDate": "bad"}]',
                                      '{"Amount": 1, "DueDate": "2026-01-01"}'])
def test_invalid_payments_field(client, payments):
    response = _post(client, payments)
    assert response.status_code == 422
    assert response.json() == {"error": InvalidPaymentsError.message}


def test_blank_payments_field_is_missing(client):
    response = _post(client, "")
    assert response.status_code == 422
    assert "'payments'" in response.json()["error"]


def test_empty_payment_list(client):
    response = _post(client, [])
    assert response.status_code == 400
    assert response.json() == {"error": "The payment list is empty."}


def test_missing_files(client):
    response = client.post("/cheques/match/upload", data={"payments": "[]"})
    assert response.status_code == 422
    assert "'files'" in response.json()["error"] and "'payments'" in response.json()["error"]


def test_parse_payments_accepts_arabic_values():
    payments = parse_payments('[{"Amount": 6250, "DueDate": "١٠/١١/٢٠٢٦"}]')
    assert payments[0].due_date.isoformat() == "2026-11-10"
    assert parse_cheque_info('{"ChequeHolderNameAr": "عمر علي"}').ChequeHolderNameAr == "عمر علي"


def test_top_level_cheque_info_applies_to_every_payment(client):
    response = _post(client, [{"RentPaymentId": 1, "Amount": 9333, "DueDate": "2026-01-01"},
                              {"RentPaymentId": 2, "Amount": 9333, "DueDate": "2026-03-01"}],
                     cheque_info={"ChequepayeeName": "Omar Aly"})
    assert response.status_code == 200
    body = response.json()
    assert body["cheque_info"]["ChequepayeeName"] == "Omar Aly"  # returned once, at the top
    assert all("cheque_info" not in p for p in body["payments"])
    assert [p["OcrResponse"] for p in body["payments"]] == [
        "Payee name doesn't match. Cheque shows Omar Ali."] * 2


@pytest.mark.parametrize("payment_extra", [{"cheque_info": {"ChequepayeeName": "Omar Ali"}},
                                           {"ChequepayeeName": "Omar Ali"}])
def test_cheque_info_inside_a_payment_is_rejected(client, payment_extra):
    response = _post(client, [{"Amount": 9333, "DueDate": "2026-01-01", **payment_extra}],
                     cheque_info={"ChequepayeeName": "Omar Ali"})
    assert response.status_code == 422
    assert response.json() == {"error": ChequeInfoInPaymentError.message}


def test_no_cheque_info_returns_null(client):
    response = _post(client, [{"Amount": 9333, "DueDate": "2026-01-01"}])
    assert response.json()["cheque_info"] is None
    assert "cheque_info" not in response.json()["payments"][0]


@pytest.mark.parametrize("cheque_info", ["not json", "[1, 2]", '{"ChequepayeeName": 5}'])
def test_invalid_cheque_info_field(client, cheque_info):
    response = client.post(
        "/cheques/match/upload",
        files=[("files", ("a.jpg", _jpeg(), "image/jpeg"))],
        data={"cheque_info": cheque_info,
              "payments": json.dumps([{"Amount": 9333, "DueDate": "2026-01-01"}])},
    )
    assert response.status_code == 422
    assert response.json() == {"error": InvalidChequeInfoError.message}


def test_cheque_info_inside_a_payment_is_rejected_on_match_json(client):
    response = client.post("/cheques/match", json={
        "file_urls": ["a.jpg"], "cheque_info": {"ChequepayeeName": "Omar Ali"},
        "payments": [{"Amount": 9333, "DueDate": "2026-01-01",
                      "cheque_info": {"ChequepayeeName": "Omar Ali"}}]})
    assert response.status_code == 422
    assert response.json() == {"error": ChequeInfoInPaymentError.message}
