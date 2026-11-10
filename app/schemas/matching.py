"""Request / response models for matching cheques against expected rent payments."""

from datetime import date
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.utils.dates import parse_date

# Keys of cheque_info, each checked against the cheque only when a value is sent
CHEQUE_INFO_KEYS = ("ChequepayeeName", "ChequeHolderName", "ChequeHolderNameAr", "ChequeHolderIBAN")
# Error type (and client message) when a payment carries cheque_info itself
CHEQUE_INFO_IN_PAYMENT = "cheque_info_in_payment"
CHEQUE_INFO_IN_PAYMENT_MESSAGE = ("Send cheque_info once at the top level of the request, "
                                  "not inside a payment.")


class ChequeInfo(BaseModel):
    """Expected cheque details (English or Arabic). Extra keys are returned unchanged."""

    model_config = ConfigDict(extra="allow")

    ChequepayeeName: Optional[str] = Field(None, description="Expected 'Pay to' name")
    ChequeHolderName: Optional[str] = Field(None, description="Expected account holder name")
    ChequeHolderNameAr: Optional[str] = Field(None, description="Expected account holder name in Arabic")
    ChequeHolderIBAN: Optional[str] = Field(None, description="Expected account holder IBAN")


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
    # The request's top-level cheque_info (set by the API; never returned on the payment)
    _shared_info: Optional[ChequeInfo] = PrivateAttr(None)

    # Filled by the API; any value sent in the request is ignored
    MatchedWithOcr: Optional[bool] = None
    MatchedOcrFileURL: Optional[str] = Field(None, description="Original file URL of the cheque")
    CroppedOCRFileURL: Optional[str] = Field(
        None, description="Cropped / rotated image of the cheque; null when no processing was needed"
    )
    OcrResponse: Optional[str] = Field(None, description="'Matched', or why it did not match")

    @model_validator(mode="before")
    @classmethod
    def _no_cheque_info_in_payment(cls, data):
        """cheque_info is sent once, at the top level. A payment carrying it (as an object or as
        the earlier flat keys) is rejected; empty / null values there are just dropped."""
        if not isinstance(data, dict):
            return data
        keys = [k for k in ("cheque_info", *CHEQUE_INFO_KEYS) if k in data]
        if not keys:
            return data
        if any(_has_value(data[k]) for k in keys):
            raise PydanticCustomError(CHEQUE_INFO_IN_PAYMENT, CHEQUE_INFO_IN_PAYMENT_MESSAGE)
        return {k: v for k, v in data.items() if k not in keys}

    def expected(self, key: str) -> str:
        """The request's cheque_info value for this key, stripped; '' when not sent."""
        info = self._shared_info
        return (getattr(info, key, None) or "").strip() if info else ""

    def expected_info(self) -> dict:
        """The cheque_info values actually checked for this payment (for the case study)."""
        return {key: self.expected(key) or None for key in CHEQUE_INFO_KEYS}

    @field_validator("DueDate")
    @classmethod
    def _valid_due_date(cls, v: str) -> str:
        if parse_date(v) is None:
            raise ValueError(f"Unrecognised DueDate: {v!r}")  # -> 422
        return v

    @field_validator("MatchedWithOcr", "MatchedOcrFileURL", "CroppedOCRFileURL", "OcrResponse",
                     mode="before")
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
    cheque_info: Optional[ChequeInfo] = Field(
        None, description="Expected cheque details for every payment: ChequepayeeName, "
                          "ChequeHolderName, ChequeHolderNameAr, ChequeHolderIBAN"
    )
    payments: list[RentPayment] = Field(..., description="Expected payments to match")

    @model_validator(mode="after")
    def _share_cheque_info(self):
        share_cheque_info(self.payments, self.cheque_info)
        return self


class MatchResponse(BaseModel):
    """Same shape as the request: cheque_info once, then the payments with their results."""
    cheque_info: Optional[ChequeInfo] = None
    payments: list[RentPayment]


def share_cheque_info(payments: list[RentPayment], info: Optional[ChequeInfo]) -> None:
    """Apply the request's top-level cheque_info to every payment."""
    for payment in payments:
        payment._shared_info = info


def _has_value(value) -> bool:
    if isinstance(value, dict):
        return any(_has_value(v) for v in value.values())
    return value is not None and str(value).strip() != ""
