from app.schemas import ChequeEntity, FileResult, RentPayment
from app.services.payment_matcher import MATCHED, match_payments


def _file(name: str, *cheques: tuple[float, str], error: str = None) -> tuple[str, FileResult]:
    return name, FileResult(
        file_name=name,
        status="success",
        cheques=[ChequeEntity(cheque_amount=a, cheque_date=d, error=error) for a, d in cheques],
    )


def _payment(pid: int, amount: float, due: str) -> RentPayment:
    return RentPayment(RentPaymentId=pid, Installment=1, ServiceId=1,
                       ServiceDescription="Property Rent", Amount=amount, DueDate=due)


def test_match_on_amount_and_date_any_format():
    payments = [_payment(1, 10000, "2024-09-27T00:00:00")]
    match_payments(payments, [_file("cheque_1.pdf", (10000, "27-09-2024"))])

    assert payments[0].MatchedWithOcr is True
    assert payments[0].MatchedOcrFile == "cheque_1.pdf"
    assert payments[0].OcrResponse == MATCHED


def test_each_cheque_used_once():
    payments = [_payment(1, 8400, "05/11/2026"), _payment(2, 8400, "05/11/2026")]
    match_payments(payments, [_file("a.pdf", (8400, "05-11-2026"))])

    assert payments[0].MatchedWithOcr is True
    assert payments[1].MatchedWithOcr is False
    assert payments[1].MatchedOcrFile is None
    assert "already matched" in payments[1].OcrResponse


def test_date_mismatch_shows_cheque_date():
    payments = [_payment(1, 5000, "2026-10-15")]
    match_payments(payments, [_file("c3.pdf", (5000, "01-10-2026"))])

    assert payments[0].MatchedWithOcr is False
    assert payments[0].MatchedOcrFile == "c3.pdf"  # closest cheque's file, even when unmatched
    assert payments[0].OcrResponse == "Date doesn't match. Cheque date is 01-10-2026."


def test_amount_mismatch_shows_cheque_amount():
    payments = [_payment(1, 4500, "2026-10-01")]
    match_payments(payments, [_file("c3.pdf", (5000, "01-10-2026"))])

    assert payments[0].OcrResponse == "Amount doesn't match. Cheque amount is 5,000."


def test_both_mismatch_shows_closest_cheque():
    payments = [_payment(1, 840, "2026-08-14")]
    match_payments(payments, [_file("c3.pdf", (5000, "01-10-2026"))])

    assert payments[0].OcrResponse == "Amount and date don't match. Cheque shows 5,000 dated 01-10-2026."


def test_overlap_note_added_when_unmatched():
    payments = [_payment(1, 840, "2026-08-14")]
    match_payments(payments, [_file("c.pdf", (5000, "01-10-2026"), error="Cheque is overlapped.")])

    assert "overlapped" in payments[0].OcrResponse


def test_no_cheques_at_all():
    payments = [_payment(1, 840, "2026-08-14")]
    match_payments(payments, [("bad.pdf", FileResult(file_name="bad.pdf", status="error", error="x"))])

    assert payments[0].OcrResponse == "None of the cheque files could be processed."


def test_input_match_fields_are_ignored():
    payment = RentPayment(Amount=1, DueDate="2026-01-01", MatchedWithOcr=True,
                          MatchedOcrFile="x", OcrResponse="y")
    assert payment.MatchedWithOcr is None
    assert payment.MatchedOcrFile is None
    assert payment.OcrResponse is None


def test_extra_keys_are_kept():
    payment = RentPayment(Amount=1, DueDate="2026-01-01", ContractNo="C-1")
    assert payment.model_dump()["ContractNo"] == "C-1"
