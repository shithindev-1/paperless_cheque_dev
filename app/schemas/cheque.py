"""Cheque entity schema — also used as the OCR model's structured-output schema."""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.utils.dates import parse_date


class ServiceType(str, Enum):
    PROPERTY_RENT = "Property Rent"
    SECURITY_DEPOSIT = "Security Deposit"
    VAT_ON_RENT = "VAT on Rent"
    HANDLING_FEE = "Handling Fee"


class ChequeEntity(BaseModel):
    cheque_date: Optional[str] = Field(None, description="Cheque date as DD-MM-YYYY")
    cheque_amount: Optional[float] = Field(None, description="Amount as a plain number")
    installment_number: Optional[int] = Field(None, description="Installment number")
    service: Optional[ServiceType] = Field(None, description="What the cheque pays for")
    error: Optional[str] = Field(
        None, description="Set only if the cheque is overlapped by another cheque or object"
    )

    @field_validator("cheque_date")
    @classmethod
    def _to_dd_mm_yyyy(cls, v: Optional[str]) -> Optional[str]:
        """Normalise any date format the OCR model returns to DD-MM-YYYY; null if unreadable."""
        parsed = parse_date(v)
        return parsed.strftime("%d-%m-%Y") if parsed else None


class ChequeExtraction(BaseModel):
    cheques: list[ChequeEntity]
