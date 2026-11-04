from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.api.deps import get_cheque_extractor
from app.core.config import Settings, get_settings
from app.schemas import MatchRequest, RentPayment
from app.services.cheque_extractor import ChequeExtractor
from app.services.matching_service import match_cheques, match_uploads

router = APIRouter(prefix="/cheques", tags=["Cheque Matching"])


@router.post("/match", response_model=list[RentPayment])
async def match_cheques_to_payments(
    request: MatchRequest,
    settings: Settings = Depends(get_settings),
    extractor: ChequeExtractor = Depends(get_cheque_extractor),
) -> list[RentPayment]:
    """Read the cheque files and match them to the expected payments by Amount + DueDate.

    Returns the payment list with MatchedWithOcr, MatchedOcrFileURL, CroppedOCRFileURL
    and OcrResponse filled. CroppedOCRFileURL is null unless the cheque was cropped / rotated.
    """
    return await match_cheques(request, settings, extractor)


@router.post("/match/upload", response_model=list[RentPayment])
async def match_uploaded_cheques_to_payments(
    files: list[UploadFile] = File(..., description="One or more cheque images / PDFs"),
    payments: str = Form(
        ...,
        description='JSON list of payments, same keys as /cheques/match, e.g. '
                    '[{"RentPaymentId": 1, "Amount": 9333, "DueDate": "2026-01-01", '
                    '"cheque_info": {"ChequepayeeName": null, "ChequeHolderName": null, '
                    '"ChequeHolderNameAr": null, "ChequeHolderIBAN": null}}]',
    ),
    settings: Settings = Depends(get_settings),
    extractor: ChequeExtractor = Depends(get_cheque_extractor),
) -> list[RentPayment]:
    """Same checks and response as /cheques/match, for files uploaded in the request (form-data).

    Verifies every payment (amount, date, payee / holder / IBAN when sent, cheque issues).
    MatchedOcrFileURL is the uploaded file name; CroppedOCRFileURL is the processed file, if any.
    """
    return await match_uploads(files, payments, settings, extractor)
