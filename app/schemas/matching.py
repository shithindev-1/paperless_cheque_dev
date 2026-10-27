"""Request / response models for matching cheques against expected rent payments."""

from datetime import date
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.utils.dates import parse_date


class RentPayment(BaseModel):
    """One expected payment. Extra keys sent by the caller are returned unchanged."""

    model_config = ConfigDict(extra="allow")

    RentPaymentId: Optional[int] = None
    Installment: Optional[int] = None
    ServiceId: Optional[int] = None
    ServiceDescription: Optional[str] = None
    Amount: Union[int, float]
    DueDate: str = Field(
        ..., description="Any common format, e.g. 2026-11-05T00:00:00, 05/11/2026, 5 Nov 2026"
    )

    # Filled by the API; any value sent in the request is ignored
    MatchedWithOcr: Optional[bool] = None
    MatchedOcrFile: Optional[str] = None
    OcrResponse: Optional[str] = Field(None, description="'Matched', or why it did not match")

    @field_validator("DueDate")
    @classmethod
    def _valid_due_date(cls, v: str) -> str:
        if parse_date(v) is None:
            raise ValueError(f"Unrecognised DueDate: {v!r}")  # -> 422
        return v

    @field_validator("MatchedWithOcr", "MatchedOcrFile", "OcrResponse", mode="before")
    @classmethod
    def _ignore_input(cls, v):
        return None

    @property
    def due_date(self) -> date:
        return parse_date(self.DueDate)


class MatchRequest(BaseModel):
    file_urls: list[str] = Field(
        ...,
        description="Azure file URLs of the cheque images / PDFs "
                    "(or file names inside LOCAL_FILES_DIR for local testing)",
    )
    payments: list[RentPayment] = Field(..., description="Expected payments to match")
