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
from app.core.tracing import RequestTrace
from app.schemas import ChequeOcr, FileResult, RentPayment
from app.services import case_study
from app.services.payment_matcher import match_payments

WORDS = "Nine Thousand Three Hundred Thirty Three Only"


def _jpeg(name: str = "a.jpg") -> bytes:
    """A blank image; each file name gets its own shade, so the files differ in content."""
    shade = 255 - 10 * (ord(name[0]) - ord("a"))
    return cv2.imencode(".jpg", np.full((400, 800, 3), shade, np.uint8))[1].tobytes()


class FakeExtractor:
    """Returns fixed cheques per uploaded file name instead of calling the OCR model."""

    def __init__(self, cheques: dict[str, list[dict]]):
        self.cheques = cheques

    async def extract_many(self, files):
        return [FileResult(file_name=f.name, status="success",
                           cheques=[ChequeOcr(**c) for c in self.cheques[f.name]]) for f in files]


def _post(client, payments, files=("a.jpg", "b.jpg"), cheque_info=None):
    form = {"payments": json.dumps(payments)}
    if cheque_info is not None:
        form["cheque_info"] = json.dumps(cheque_info)
    return client.post(
        "/cheques/match/upload",
        files=[("files", (name, _jpeg(name), "image/jpeg")) for name in files],
        data=form,
    )


_WORDS_FOR = {100: "One Hundred Only", 200: "Two Hundred Only"}


def _cheque(amount, date) -> ChequeOcr:
    return ChequeOcr(cheque_amount=amount, cheque_date=date, amount_in_words=_WORDS_FOR[amount])


def _payment(pid, amount, due) -> RentPayment:
    return RentPayment(RentPaymentId=pid, Amount=amount, DueDate=due)


# ── build_cases (no I/O) ──

def test_unmatched_payment_becomes_a_case_for_its_cheque():
    results = [FileResult(file_name="a.jpg", status="success",
                          cheques=[_cheque(100, "01-01-2026"), _cheque(200, "01-02-2026")])]
    payments = [_payment(1, 100, "2026-01-01"), _payment(2, 250, "2026-02-01")]
    misses = []
    match_payments(payments, [("a.jpg", results[0])], misses)

    cases, orphans = case_study.build_cases(["a.jpg"], results, payments, misses, "rid", "POST /x")

    assert orphans == []
    [case] = cases
    assert (case.file_index, case.cheque_index) == (0, 1)
    assert case.record["file"] == {"name": "a.jpg", "source": "a.jpg", "cheque_index": 1,
                                   "cheques_in_file": 2}
    assert case.record["ocr_read"]["amount"] == 200
    assert case.record["ocr_read"]["date"] == "01-02-2026"
    [failed] = case.record["failed_payments"]
    assert failed["RentPaymentId"] == 2
    assert failed["OcrResponse"].startswith("Amount doesn't match")
    assert "sharpness" not in json.dumps(case.record) and "model" not in case.record


def test_unreadable_file_is_a_case_with_the_payments_that_found_no_cheque():
    results = [FileResult(file_name="bad.pdf", status="error", error="The file could not be read."),
               FileResult(file_name="b.jpg", status="success", cheques=[_cheque(100, "01-01-2026")])]
    payments = [_payment(1, 100, "2026-01-01"), _payment(2, 300, "2026-03-01")]
    misses = []
    match_payments(payments, [("bad.pdf", results[0]), ("b.jpg", results[1])], misses)

    cases, orphans = case_study.build_cases(["https://h/c/bad.pdf?sig=SECRET", "b.jpg"],
                                            results, payments, misses, "rid", None)

    [case] = cases
    assert case.cheque_index is None and "ocr_read" not in case.record
    assert case.record["error"] == "The file could not be read."
    assert case.record["file"]["source"] == "https://h/c/bad.pdf"  # SAS token dropped
    assert [p["RentPaymentId"] for p in case.record["failed_payments"]] == [2]
    assert orphans == []


def test_payment_with_no_file_to_point_at_is_an_orphan():
    results = [FileResult(file_name="a.jpg", status="success", cheques=[_cheque(100, "01-01-2026")])]
    payments = [_payment(1, 100, "2026-01-01"), _payment(2, 100, "2026-01-01")]
    misses = []
    match_payments(payments, [("a.jpg", results[0])], misses)

    cases, orphans = case_study.build_cases(["a.jpg"], results, payments, misses, "rid", None)
    assert cases == [] and [p["RentPaymentId"] for p in orphans] == [2]


def test_nothing_failed_means_no_job(settings):
    settings.case_study_folder = "CaseStudy"
    results = [FileResult(file_name="a.jpg", status="success", cheques=[_cheque(100, "01-01-2026")])]
    payments = [_payment(1, 100, "2026-01-01")]
    misses = []
    match_payments(payments, [("a.jpg", results[0])], misses)
    trace = RequestTrace("POST", "/x", None, None, {})
    assert case_study.prepare(["a.jpg"], results, {}, payments, misses, settings, trace) is None


# ── through the endpoint (background task runs before TestClient returns) ──

@pytest.fixture
def client(settings):
    settings.processed_folder = "processed"
    settings.case_study_folder = "CaseStudy"
    extractor = FakeExtractor({
        "a.jpg": [{"cheque_date": "01-01-2026", "cheque_amount": "9333", "amount_in_words": WORDS,
                   "page": "1", "bounding_box": [100, 100, 900, 900], "text_top": "up",
                   "needs_crop": True}],
        "b.jpg": [{"cheque_date": "01-03-2026", "cheque_amount": "9333", "amount_in_words": WORDS}],
    })
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(matching.router)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_cheque_extractor] = lambda: extractor
    return TestClient(app)


def _folders(tmp_path):
    root = tmp_path / "CaseStudy"
    return [p for p in root.iterdir() if p.name not in ("_index", "_seen")] if root.exists() else []


def test_failed_payments_are_saved_with_original_crop_and_case_json(client, tmp_path):
    response = _post(client, [
        {"RentPaymentId": 1, "Amount": 9000, "DueDate": "2026-01-01"},  # a.jpg, wrong amount
        {"RentPaymentId": 2, "Amount": 9333, "DueDate": "2026-05-01"},  # b.jpg, wrong date
    ], cheque_info={"ChequeHolderIBAN": "AE070331234567890123456"})
    assert response.status_code == 200
    assert [p["MatchedWithOcr"] for p in response.json()["payments"]] == [False, False]

    [folder] = _folders(tmp_path)
    assert sorted(p.name for p in folder.iterdir()) == [
        "a_case.json", "a_ocr.pdf", "b.jpg", "b_case.json"]  # a cropped: no original; b: original
    assert (folder / "a_ocr.pdf").read_bytes().startswith(b"%PDF")
    a_case = json.loads((folder / "a_case.json").read_text(encoding="utf-8"))
    assert a_case["file"]["original"] is None
    assert a_case["file"]["cropped"] == f"CaseStudy/{folder.name}/a_ocr.pdf"
    assert (folder / "b.jpg").read_bytes() == _jpeg("b.jpg")

    case = json.loads((folder / "b_case.json").read_text(encoding="utf-8"))
    assert case["request_id"] == folder.name
    assert case["file"]["original"] == f"CaseStudy/{folder.name}/b.jpg"
    assert case["file"]["cropped"] is None
    [failed] = case["failed_payments"]
    assert failed["cheque_info"]["ChequeHolderIBAN"] == "AE070331234567890123456"  # full IBAN
    assert failed["OcrResponse"] == ("Date doesn't match. Cheque date is 01-03-2026. "
                                     "IBAN not found on the cheque.")
    assert case["ocr_read"]["date"] == "01-03-2026"

    [index] = (tmp_path / "CaseStudy" / "_index").iterdir()
    lines = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines()]
    assert [(line["file"], line["RentPaymentId"]) for line in lines] == [("a.jpg", 1), ("b.jpg", 2)]
    assert all(line["request_id"] == folder.name for line in lines)


def test_matched_request_saves_nothing(client, tmp_path):
    response = _post(client, [{"RentPaymentId": 1, "Amount": 9333, "DueDate": "2026-01-01"},
                              {"RentPaymentId": 2, "Amount": 9333, "DueDate": "2026-03-01"}])
    assert [p["MatchedWithOcr"] for p in response.json()["payments"]] == [True, True]
    assert _folders(tmp_path) == []


def test_case_study_off(client, settings, tmp_path):
    settings.case_study_enabled = False
    _post(client, [{"RentPaymentId": 1, "Amount": 1, "DueDate": "2026-01-01"}])
    assert _folders(tmp_path) == []


def test_saving_errors_never_change_the_response(client, monkeypatch, tmp_path):
    def broken(*args):
        raise RuntimeError("storage down")
    monkeypatch.setattr(case_study, "_store_for", broken)

    response = _post(client, [{"RentPaymentId": 1, "Amount": 1, "DueDate": "2026-01-01"}])
    assert response.status_code == 200
    assert response.json()["payments"][0]["MatchedWithOcr"] is False
    assert _folders(tmp_path) == []


def test_same_file_failing_again_is_only_indexed(client, tmp_path):
    payments = [{"RentPaymentId": 1, "Amount": 9333, "DueDate": "2026-05-01"}]  # a.jpg, wrong date
    _post(client, payments, files=("a.jpg",))
    _post(client, payments, files=("a.jpg",))  # same request again: new request id

    [folder] = _folders(tmp_path)  # saved once
    [index] = (tmp_path / "CaseStudy" / "_index").iterdir()
    first, again = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines()]
    assert first["repeat_of"] is None and first["case_file"]
    assert again["repeat_of"] == folder.name and again["case_file"] is None
    assert again["request_id"] != folder.name
    assert again["OcrResponse"] == first["OcrResponse"]
    assert len(list((tmp_path / "CaseStudy" / "_seen").iterdir())) == 1


def test_different_file_with_same_failure_is_saved_again(client, tmp_path):
    payments = [{"RentPaymentId": 1, "Amount": 9333, "DueDate": "2026-05-01"}]
    _post(client, payments, files=("a.jpg",))
    client.post("/cheques/match/upload",  # same name, amount and date; different image
                files=[("files", ("a.jpg", cv2.imencode(".jpg", np.zeros((400, 800, 3), np.uint8))[1]
                                  .tobytes(), "image/jpeg"))],
                data={"payments": json.dumps(payments)})
    assert len(_folders(tmp_path)) == 2
