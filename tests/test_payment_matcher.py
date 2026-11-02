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
    assert payments[0].MatchedOcrFileURL == "cheque_1.pdf"
    assert payments[0].CroppedOCRFileURL is None  # not processed
    assert payments[0].OcrResponse == MATCHED


def test_each_cheque_used_once():
    payments = [_payment(1, 8400, "05/11/2026"), _payment(2, 8400, "05/11/2026")]
    match_payments(payments, [_file("a.pdf", (8400, "05-11-2026"))])

    assert payments[0].MatchedWithOcr is True
    assert payments[1].MatchedWithOcr is False
    assert payments[1].MatchedOcrFileURL is None
    assert payments[1].CroppedOCRFileURL is None
    assert "already matched" in payments[1].OcrResponse


def test_date_mismatch_shows_cheque_date():
    payments = [_payment(1, 5000, "2026-10-15")]
    match_payments(payments, [_file("c3.pdf", (5000, "01-10-2026"))])

    assert payments[0].MatchedWithOcr is False
    assert payments[0].MatchedOcrFileURL == "c3.pdf"  # closest cheque's file, even when unmatched
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
                          MatchedOcrFileURL="x", CroppedOCRFileURL="z", OcrResponse="y")
    assert payment.MatchedWithOcr is None
    assert payment.MatchedOcrFileURL is None
    assert payment.CroppedOCRFileURL is None
    assert payment.OcrResponse is None


def test_extra_keys_are_kept():
    payment = RentPayment(Amount=1, DueDate="2026-01-01", ContractNo="C-1")
    assert payment.model_dump()["ContractNo"] == "C-1"


def test_only_matched_cheques_are_returned_for_cropping():
    files = [_file("a.pdf", (100, "01-01-2026")),
             _file("multi.pdf", (8400, "05-11-2026"), (840, "05-11-2026"))]
    payments = [_payment(1, 840, "2026-11-05"), _payment(2, 999, "2026-11-05")]
    matches = match_payments(payments, files)

    assert [(m.payment.RentPaymentId, m.file_index, m.cheque_index) for m in matches] == [(1, 1, 1)]
    assert payments[0].MatchedOcrFileURL == "multi.pdf"
    assert payments[1].MatchedWithOcr is False
    assert payments[1].CroppedOCRFileURL is None  # unmatched: never cropped


def test_layout_fields_never_serialised():
    from app.schemas import ChequeOcr

    cheque = ChequeOcr(cheque_amount=1, bounding_box=[0, 0, 10, 10], text_top="left",
                       needs_crop=True, page=1)
    result = FileResult(file_name="a.jpeg", status="success", cheques=[cheque],
                        cheque_files=["a_ocr.pdf"])
    dumped = result.model_dump()
    assert set(dumped["cheques"][0]) == {"cheque_date", "cheque_amount", "installment_number",
                                         "service", "payee_name", "holder_name", "iban", "error",
                                         "amount_in_words", "unclear_fields", "issues"}
    assert "cheque_files" not in dumped


# ── payee / holder / IBAN ──

def _named_file(**fields) -> tuple[str, FileResult]:
    cheque = ChequeEntity(**{"cheque_amount": 8400, "cheque_date": "05-11-2026", **fields})
    return "n.pdf", FileResult(file_name="n.pdf", status="success", cheques=[cheque])


def _named_payment(**fields) -> RentPayment:
    return RentPayment(Amount=8400, DueDate="2026-11-05", **fields)


def test_names_not_sent_are_not_checked():
    payments = [_named_payment()]
    match_payments(payments, [_named_file(payee_name="ABC LLC")])
    assert payments[0].OcrResponse == MATCHED


def test_names_and_iban_match_ignoring_case_and_spaces():
    payments = [_named_payment(ChequepayeeName="abc  llc", ChequeHolderName="AHMED ALI",
                               ChequeHolderIBAN="AE070331234567890123456")]
    match_payments(payments, [_named_file(payee_name="ABC LLC", holder_name="Ahmed Ali",
                                          iban="AE07 0331 2345 6789 0123 456")])
    assert payments[0].MatchedWithOcr is True


def test_name_mismatch_is_strict_and_reported():
    payments = [_named_payment(ChequepayeeName="ABC L.L.C.", ChequeHolderIBAN="AE07")]
    match_payments(payments, [_named_file(payee_name="ABC LLC")])
    assert payments[0].MatchedWithOcr is False
    assert payments[0].MatchedOcrFileURL == "n.pdf"
    assert payments[0].OcrResponse == ("Payee name doesn't match. Cheque shows ABC LLC. "
                                       "IBAN not found on the cheque.")


def test_name_mismatch_added_to_date_mismatch():
    payments = [RentPayment(Amount=8400, DueDate="2026-12-05", ChequeHolderName="X")]
    match_payments(payments, [_named_file(holder_name="Y")])
    assert payments[0].OcrResponse == ("Date doesn't match. Cheque date is 05-11-2026. "
                                       "Holder name doesn't match. Cheque shows Y.")


def test_unclear_field_is_not_compared_and_asks_for_clear_copy():
    payments = [_named_payment(ChequepayeeName="ABC LLC", ChequeHolderName="Ahmed Ali")]
    match_payments(payments, [_named_file(payee_name=None, holder_name="Ahmed Ali",
                                          unclear_fields=["payee_name"])])
    assert payments[0].MatchedWithOcr is False
    assert payments[0].OcrResponse == "Payee name is not clearly visible. Please upload a clear copy."


def test_unclear_amount_and_date():
    payments = [_named_payment()]
    match_payments(payments, [_named_file(cheque_amount=None, cheque_date=None,
                                          unclear_fields=["cheque_amount", "cheque_date"])])
    assert payments[0].OcrResponse == ("Amount and date are not clearly visible. "
                                       "Please upload a clear copy.")


# ── cheque checks (OCR output goes through ChequeOcr validation) ──

def _ocr_cheque(**fields):
    from app.core.timezones import now_uae
    from app.schemas import ChequeOcr

    base = {"cheque_amount": 8400, "cheque_date": now_uae().strftime("%d-%m-%Y"),
            "amount_in_words": "Eight Thousand Four Hundred Only", "amount_in_words_value": 8400}
    return ChequeOcr(**{**base, **fields})


def _match_one(cheque, **payment_fields) -> RentPayment:
    payment = RentPayment(Amount=8400, DueDate=cheque.cheque_date or "2026-01-01", **payment_fields)
    match_payments([payment], [("n.pdf", FileResult(file_name="n.pdf", status="success", cheques=[cheque]))])
    return payment


def test_clean_cheque_matches():
    cheque = _ocr_cheque()
    assert cheque.issues == [] and cheque.error is None
    assert _match_one(cheque).OcrResponse == MATCHED


def test_words_must_agree_with_figures():
    cheque = _ocr_cheque(amount_in_words="Eight Thousand Only", amount_in_words_value=8000)
    expected = "Amount in words (Eight Thousand Only) does not match figures (8,400)."
    assert cheque.error == expected
    payment = _match_one(cheque)
    assert payment.MatchedWithOcr is False
    assert payment.OcrResponse == expected


def test_model_amount_mismatch_ignored_when_numbers_agree():
    cheque = _ocr_cheque(issues=[{"code": "AMOUNT_MISMATCH"}])
    assert _match_one(cheque).OcrResponse == MATCHED


def test_stale_cheque_is_rejected():
    cheque = _ocr_cheque(cheque_date="01-01-2020")
    assert _match_one(cheque).OcrResponse == ("Cheque date is not valid "
                                              "(dated 01-01-2020, more than 6 months old).")


def test_model_issues_reported_with_clear_copy_when_needed():
    cheque = _ocr_cheque(issues=[{"code": "MIXED_LANGUAGE"},
                                 {"code": "ALTERATION", "detail": "Amount in figures"},
                                 {"code": "SIGNATURE_MISSING"},
                                 {"code": "DAMAGED", "detail": "near the MICR line"}])
    assert _match_one(cheque).OcrResponse == (  # signature / words / damage / alteration first
        "Signature missing. Cheque appears damaged near the MICR line. "
        "Overwriting or strike-through found on the amount in figures. "
        "UAE banks do not accept altered cheques. Please provide a new cheque. "
        "Cheque is filled in a mix of English and Arabic. "
        "Please upload a clear copy.")


def test_alteration_is_rejected_even_when_amount_and_date_match():
    payment = _match_one(_ocr_cheque(issues=[{"code": "ALTERATION", "detail": "payee name"}]))
    assert payment.MatchedWithOcr is False
    assert payment.OcrResponse == ("Overwriting or strike-through found on the payee name. "
                                   "UAE banks do not accept altered cheques. Please provide a new cheque.")


def test_cheque_issues_come_before_amount_and_date():
    cheque = _ocr_cheque(issues=[{"code": "SIGNATURE_MISSING"}])
    payment = RentPayment(Amount=5000, DueDate=cheque.cheque_date)
    match_payments([payment], [("n.pdf", FileResult(file_name="n.pdf", status="success",
                                                    cheques=[cheque]))])
    assert payment.OcrResponse == "Signature missing. Amount doesn't match. Cheque amount is 8,400."


def test_unclear_field_not_repeated_as_issue():
    cheque = _ocr_cheque(payee_name=None, unclear_fields=["payee_name"],
                         issues=[{"code": "PAYEE_ISSUE"}, {"code": "ILLEGIBLE"}])
    assert _match_one(cheque).OcrResponse == ("Payee name is not clearly visible. "
                                              "Please upload a clear copy.")


def test_unclear_holder_only_matters_when_sent():
    cheque = _ocr_cheque(holder_name=None, unclear_fields=["holder_name"])
    assert _match_one(cheque).OcrResponse == MATCHED
    assert _match_one(cheque, ChequeHolderName="A").OcrResponse == (
        "Holder name is not clearly visible. Please upload a clear copy.")


def test_name_check_picks_the_right_cheque():
    payments = [_named_payment(ChequepayeeName="B")]
    source, result = _named_file(payee_name="A")
    result.cheques.append(ChequeEntity(cheque_amount=8400, cheque_date="05-11-2026", payee_name="B"))
    matches = match_payments(payments, [(source, result)])
    assert payments[0].MatchedWithOcr is True
    assert matches[0].cheque_index == 1


def test_unclear_value_is_dropped_never_compared():
    cheque = _ocr_cheque(cheque_date="09-10-2023", unclear_fields=["cheque_date"])
    assert cheque.cheque_date is None  # the model's guess is discarded
    assert "Date doesn't match" not in _match_one(cheque).OcrResponse


def test_blurred_cheque_only_asks_for_clear_copy():
    cheque = _ocr_cheque(unclear_fields=["cheque_date"],
                         issues=[{"code": "IMAGE_INCOMPLETE", "detail": "Image is extremely blurry"}])
    payment = RentPayment(Amount=5000, DueDate="2027-03-05", ChequepayeeName="Someone")  # not compared
    match_payments([payment], [("b.jpg", FileResult(file_name="b.jpg", status="success",
                                                    cheques=[cheque]))])
    assert payment.MatchedWithOcr is False
    assert payment.OcrResponse == "Cheque image is incomplete or unclear. Please upload a clear, full copy."


def test_cut_off_cheque_gets_one_short_message():
    cheque = _ocr_cheque(cheque_date=None, cheque_amount=None, issues=[
        {"code": "SIGNATURE_MISSING"},
        {"code": "IMAGE_INCOMPLETE",
         "detail": "Right side of the cheque including date, amount in figures, and signature is cropped out"},
        {"code": "DATE_ISSUE", "detail": "Date field is cropped out"}])
    assert _match_one(cheque).OcrResponse == ("Cheque image is incomplete or unclear. "
                                              "Please upload a clear, full copy.")


def test_long_model_detail_is_dropped():
    cheque = _ocr_cheque(issues=[{"code": "ALTERATION",
                                  "detail": "the amount in figures appears to have been written over twice"}])
    assert _match_one(cheque).OcrResponse == ("Overwriting or strike-through found. "
                                              "UAE banks do not accept altered cheques. "
                                              "Please provide a new cheque.")
