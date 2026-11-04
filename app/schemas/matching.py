"""Request / response models for matching cheques against expected rent payments."""

from datetime import date
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.utils.dates import parse_date

# Keys of cheque_info, each checked against the cheque only when a value is sent
CHEQUE_INFO_KEYS = ("ChequepayeeName", "ChequeHolderName", "ChequeHolderNameAr", "ChequeHolderIBAN")


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
    # Optional: payee / holder / IBAN, checked against the cheque only when a value is sent
    cheque_info: Optional[ChequeInfo] = Field(
        None, description="Expected cheque details: ChequepayeeName, ChequeHolderName, "
                          "ChequeHolderNameAr, ChequeHolderIBAN"
    )

    # Filled by the API; any value sent in the request is ignored
    MatchedWithOcr: Optional[bool] = None
    MatchedOcrFileURL: Optional[str] = Field(None, description="Original file URL of the cheque")
    CroppedOCRFileURL: Optional[str] = Field(
        None, description="Cropped / rotated image of the cheque; null when no processing was needed"
    )
    OcrResponse: Optional[str] = Field(None, description="'Matched', or why it did not match")

    @model_validator(mode="before")
    @classmethod
    def _flat_keys_into_cheque_info(cls, data):
        """Earlier format: the keys sat on the payment itself. Moved into cheque_info
        (a value already in cheque_info wins)."""
        if not isinstance(data, dict) or not any(k in data for k in CHEQUE_INFO_KEYS):
            return data
        data = dict(data)
        info = data.get("cheque_info")
        info = dict(info) if isinstance(info, dict) else {}
        for key in CHEQUE_INFO_KEYS:
            value = data.pop(key, None)
            if info.get(key) is None and value is not None:
                info[key] = value
        data["cheque_info"] = info
        return data

    def expected(self, key: str) -> str:
        """The value sent for a cheque_info key, stripped; '' when not sent."""
        return (getattr(self.cheque_info, key, None) or "").strip() if self.cheque_info else ""

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
    payments: list[RentPayment] = Field(..., description="Expected payments to match")
