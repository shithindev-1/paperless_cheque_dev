from fastapi import APIRouter, Depends

from app.api.deps import get_cheque_extractor
from app.core.config import Settings, get_settings
from app.schemas import MatchRequest, RentPayment
from app.services.cheque_extractor import ChequeExtractor
from app.services.matching_service import match_cheques

router = APIRouter(prefix="/cheques", tags=["Cheque Matching"])


@router.post("/match", response_model=list[RentPayment])
async def match_cheques_to_payments(
    request: MatchRequest,
    settings: Settings = Depends(get_settings),
    extractor: ChequeExtractor = Depends(get_cheque_extractor),
) -> list[RentPayment]:
    """Read the cheque files and match them to the expected payments by Amount + DueDate.

    Returns the payment list with MatchedWithOcr, MatchedOcrFile and OcrResponse filled.
    """
    return await match_cheques(request, settings, extractor)
