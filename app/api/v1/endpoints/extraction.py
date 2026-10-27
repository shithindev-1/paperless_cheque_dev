from fastapi import APIRouter, Depends, File, UploadFile

from app.api.deps import get_cheque_extractor
from app.core.config import Settings, get_settings
from app.schemas import BatchResponse
from app.services.cheque_extractor import ChequeExtractor
from app.services.extraction_service import extract_uploads

router = APIRouter(prefix="/cheques", tags=["Cheque OCR"])


@router.post("/extract", response_model=BatchResponse)
async def extract_cheques(
    files: list[UploadFile] = File(..., description="One or more cheque images / PDFs"),
    settings: Settings = Depends(get_settings),
    extractor: ChequeExtractor = Depends(get_cheque_extractor),
) -> BatchResponse:
    """Extract cheque date, amount, installment number and service from uploaded files."""
    return await extract_uploads(files, settings, extractor)
