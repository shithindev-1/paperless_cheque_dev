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


class ChequeOcr(ChequeEntity):
    """What the OCR model returns per cheque: the entity plus layout for crop / rotation.

    Layout fields are internal: responses are typed as ChequeEntity, so they are never
    serialised to clients.
    """

    page: Optional[int] = Field(None, description="1-based page number (1 for images)")
    bounding_box: Optional[list[int]] = Field(
        None, description="[ymin, xmin, ymax, xmax] of the whole cheque, normalised 0-1000"
    )
    rotation_degrees: Optional[float] = Field(
        None, description="Clockwise degrees to rotate the cheque so its text is upright"
    )
    needs_crop: Optional[bool] = Field(
        None, description="True if the image contains much more than this cheque"
    )


class ChequeExtraction(BaseModel):
    cheques: list[ChequeOcr]
