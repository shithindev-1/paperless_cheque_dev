"""Cheque entity schema — also used as the OCR model's structured-output schema."""

import calendar
from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.timezones import now_uae
from app.utils.dates import parse_date

STALE_AFTER_MONTHS = 6
CLEAR_COPY = "Please upload a clear copy."
UNREADABLE_MESSAGE = "Cheque image is incomplete or unclear. Please upload a clear, full copy."
MAX_DETAIL_LENGTH = 45  # longer model details are dropped; the base message is enough


class ServiceType(str, Enum):
    PROPERTY_RENT = "Property Rent"
    SECURITY_DEPOSIT = "Security Deposit"
    VAT_ON_RENT = "VAT on Rent"
    HANDLING_FEE = "Handling Fee"


class UnclearField(str, Enum):
    CHEQUE_DATE = "cheque_date"
    CHEQUE_AMOUNT = "cheque_amount"
    AMOUNT_IN_WORDS = "amount_in_words"
    PAYEE_NAME = "payee_name"
    HOLDER_NAME = "holder_name"
    IBAN = "iban"


FIELD_LABELS = {
    UnclearField.CHEQUE_DATE: "Date",
    UnclearField.CHEQUE_AMOUNT: "Amount",
    UnclearField.AMOUNT_IN_WORDS: "Amount in words",
    UnclearField.PAYEE_NAME: "Payee name",
    UnclearField.HOLDER_NAME: "Holder name",
    UnclearField.IBAN: "IBAN",
}

# Fields every cheque must have readable; holder name / IBAN only matter when the caller sends them
CORE_FIELDS = (UnclearField.CHEQUE_DATE, UnclearField.CHEQUE_AMOUNT,
               UnclearField.AMOUNT_IN_WORDS, UnclearField.PAYEE_NAME)


class IssueCode(str, Enum):
    ALTERATION = "ALTERATION"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    AMOUNT_MISSING = "AMOUNT_MISSING"
    DATE_ISSUE = "DATE_ISSUE"
    PAYEE_ISSUE = "PAYEE_ISSUE"
    SIGNATURE_MISSING = "SIGNATURE_MISSING"
    MIXED_LANGUAGE = "MIXED_LANGUAGE"
    DAMAGED = "DAMAGED"
    ILLEGIBLE = "ILLEGIBLE"
    IMAGE_INCOMPLETE = "IMAGE_INCOMPLETE"


ISSUE_MESSAGES = {
    IssueCode.ALTERATION: "Overwriting or strike-through found",
    IssueCode.AMOUNT_MISMATCH: "Amount in words does not match figures",
    IssueCode.AMOUNT_MISSING: "Amount in words or figures is missing",
    IssueCode.DATE_ISSUE: "Cheque date is not valid",
    IssueCode.PAYEE_ISSUE: "Payee name is missing or overwritten",
    IssueCode.SIGNATURE_MISSING: "Signature missing",
    IssueCode.MIXED_LANGUAGE: "Cheque is filled in a mix of English and Arabic",
    IssueCode.DAMAGED: "Cheque appears damaged",
    IssueCode.ILLEGIBLE: "Handwriting is not clear",
    IssueCode.IMAGE_INCOMPLETE: "Cheque image is incomplete or blurred",
}

# Checked first: always listed before any other reason in OcrResponse
FIRST_ISSUES = (IssueCode.SIGNATURE_MISSING, IssueCode.AMOUNT_MISMATCH, IssueCode.DAMAGED,
                IssueCode.ALTERATION)

# UAE banks reject any altered cheque, even if initialled: a clear copy does not help
ALTERATION_NOTE = "UAE banks do not accept altered cheques. Please provide a new cheque."

# The image itself is unreliable: values are not compared, only a clear copy is requested
UNREADABLE_ISSUES = {IssueCode.IMAGE_INCOMPLETE, IssueCode.ILLEGIBLE}

# Issues that a clearer upload can fix
_NEEDS_CLEAR_COPY = {IssueCode.ILLEGIBLE, IssueCode.IMAGE_INCOMPLETE, IssueCode.DAMAGED}

# An issue already explained by its field being unclear is not repeated
_COVERED_BY_UNCLEAR = {
    IssueCode.ILLEGIBLE: set(CORE_FIELDS),
    IssueCode.AMOUNT_MISSING: {UnclearField.CHEQUE_AMOUNT, UnclearField.AMOUNT_IN_WORDS},
    IssueCode.DATE_ISSUE: {UnclearField.CHEQUE_DATE},
    IssueCode.PAYEE_ISSUE: {UnclearField.PAYEE_NAME},
}


class ChequeIssue(BaseModel):
    code: IssueCode
    detail: Optional[str] = Field(None, description="A few words, e.g. 'amount in figures overwritten'")

    @property
    def message(self) -> str:
        base = ISSUE_MESSAGES[self.code]
        detail = (self.detail or "").strip().rstrip(".")
        if self.code == IssueCode.ALTERATION:  # detail is the field: "on the payee name."
            where = f" on the {detail.lower()}" if detail and len(detail) <= MAX_DETAIL_LENGTH else ""
            return f"{base}{where}. {ALTERATION_NOTE}"
        if self.code == IssueCode.SIGNATURE_MISSING or not detail:
            return f"{base}."
        if self.code == IssueCode.AMOUNT_MISMATCH and detail.lower().startswith("amount in words"):
            return f"{detail}."  # full sentence built from both values
        if len(detail) > MAX_DETAIL_LENGTH:  # long model explanations make the message hard to read
            return f"{base}."
        if self.code == IssueCode.DAMAGED:  # detail is a location: "near the MICR line"
            return f"{base} {detail}."
        return f"{base} ({detail})."


def unclear_message(fields: list[UnclearField]) -> str:
    """'Date and payee name are not clearly visible.'"""
    labels = [FIELD_LABELS[UnclearField(f)] for f in dict.fromkeys(fields)]
    labels = [labels[0]] + [label if label == "IBAN" else label[0].lower() + label[1:]
                            for label in labels[1:]]
    text = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
    return f"{text} {'is' if len(labels) == 1 else 'are'} not clearly visible."


def _months_before(day: date, months: int) -> date:
    month_index = day.year * 12 + day.month - 1 - months
    year, month = divmod(month_index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _fmt_amount(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


class ChequeEntity(BaseModel):
    cheque_date: Optional[str] = Field(None, description="Cheque date as DD-MM-YYYY")
    cheque_amount: Optional[float] = Field(None, description="Amount in figures as a plain number")
    amount_in_words: Optional[str] = Field(None, description="Amount written in words, as written")
    installment_number: Optional[int] = Field(None, description="Installment number")
    service: Optional[ServiceType] = Field(None, description="What the cheque pays for")
    payee_name: Optional[str] = Field(None, description="Name on the 'Pay to' line")
    holder_name: Optional[str] = Field(None, description="Account holder name printed on the cheque")
    iban: Optional[str] = Field(None, description="IBAN printed on the cheque")
    unclear_fields: list[UnclearField] = Field(
        default_factory=list, description="Fields on the cheque that are not clearly readable"
    )
    issues: list[ChequeIssue] = Field(
        default_factory=list, description="Problems that make the cheque unacceptable"
    )
    error: Optional[str] = Field(
        None, description="Set only if the cheque is overlapped by another cheque or object"
    )

    @field_validator("cheque_date")
    @classmethod
    def _to_dd_mm_yyyy(cls, v: Optional[str]) -> Optional[str]:
        """Normalise any date format the OCR model returns to DD-MM-YYYY; null if unreadable."""
        parsed = parse_date(v)
        return parsed.strftime("%d-%m-%Y") if parsed else None

    @property
    def unreadable(self) -> bool:
        """Blurred / incomplete / illegible: no value on this cheque can be trusted for comparison."""
        return any(i.code in UNREADABLE_ISSUES for i in self.issues)

    def unclear(self, extra_unclear: tuple[UnclearField, ...] = ()) -> list[UnclearField]:
        """Unclear core fields, plus unclear holder name / IBAN when the caller sent them."""
        return [f for f in self.unclear_fields if f in CORE_FIELDS] + list(extra_unclear)

    def issue_messages(self, unclear: list[UnclearField]) -> list[str]:
        """Issue messages, signature / words vs figures / damage first.

        A cut-off / blurred / illegible cheque gets one short message: everything else
        (missing signature, date, amount, ...) is only a consequence of the bad image.
        """
        if self.unreadable:
            return [UNREADABLE_MESSAGE]
        issues = [i for i in self.issues
                  if not _COVERED_BY_UNCLEAR.get(i.code, set()) & set(unclear)]
        issues.sort(key=lambda i: FIRST_ISSUES.index(i.code) if i.code in FIRST_ISSUES
                    else len(FIRST_ISSUES))
        return [i.message for i in issues]

    def unclear_messages(self, unclear: list[UnclearField]) -> list[str]:
        """'X is not clearly visible.' and the clear-copy request, when either applies."""
        if self.unreadable:  # already in UNREADABLE_MESSAGE
            return []
        messages = [unclear_message(unclear)] if unclear else []
        if unclear or any(i.code in _NEEDS_CLEAR_COPY for i in self.issues):
            messages.append(CLEAR_COPY)
        return messages

    def problems(self, extra_unclear: tuple[UnclearField, ...] = ()) -> list[str]:
        """Client messages for everything that makes this cheque unacceptable (empty = fine)."""
        unclear = self.unclear(extra_unclear)
        return self.issue_messages(unclear) + self.unclear_messages(unclear)


class TextTop(str, Enum):
    """Where the top of the printed text points. Easier for the model than a signed angle."""
    UP = "up"
    RIGHT = "right"
    DOWN = "down"
    LEFT = "left"


# Clockwise turn that brings the text upright
_QUARTER_TURN = {TextTop.UP: 0, TextTop.RIGHT: -90, TextTop.DOWN: 180, TextTop.LEFT: 90}


class ChequeOcr(ChequeEntity):
    """What the OCR model returns per cheque: the entity plus layout for crop / rotation.

    Layout fields are internal: responses are typed as ChequeEntity, so they are never
    serialised to clients.
    """

    page: Optional[int] = Field(None, description="1-based page number (1 for images)")
    bounding_box: Optional[list[int]] = Field(
        None, description="[ymin, xmin, ymax, xmax] of the whole cheque, normalised 0-1000"
    )
    text_top: Optional[TextTop] = Field(
        None, description="Direction the top of the cheque's printed text points in the image"
    )
    tilt_degrees: Optional[float] = Field(
        None, description="Small clockwise tilt (-45 to 45) still needed once the text points up"
    )
    needs_crop: Optional[bool] = Field(
        None, description="True if the image contains much more than this cheque"
    )
    amount_in_words_value: Optional[float] = Field(
        None, description="The number the amount in words states, e.g. 'Five Thousand Only' -> 5000"
    )

    @property
    def quarter_turn(self) -> int:
        """Clockwise quarter turn that makes the text upright (0, 90, 180 or -90)."""
        return _QUARTER_TURN.get(self.text_top, 0)

    @property
    def rotation_degrees(self) -> float:
        """Total clockwise rotation the model suggests: quarter turn plus tilt."""
        return self.quarter_turn + (self.tilt_degrees or 0.0)

    @model_validator(mode="after")
    def _check_cheque(self) -> "ChequeOcr":
        """Add the checks the backend can do itself, then report everything in `error`."""
        issues = {i.code: i for i in self.issues}
        unclear = set(self.unclear_fields)

        # Never keep a value the model itself marked as unclear: it would be a guess
        for field in unclear:
            setattr(self, field.value, None)
        if unclear & {UnclearField.AMOUNT_IN_WORDS, UnclearField.CHEQUE_AMOUNT}:
            self.amount_in_words_value = None

        if not issues.keys() & UNREADABLE_ISSUES:  # nothing is derived from a bad image
            self._derive_issues(issues, unclear)

        self.issues = list(issues.values())
        notes = ([self.error] if self.error else []) + self.problems()
        self.error = " ".join(notes) or None
        return self

    def as_unreadable(self, detail: str) -> "ChequeOcr":
        """Same position on the page, every value dropped: the image is too poor to trust any of it."""
        return ChequeOcr(page=self.page, bounding_box=self.bounding_box,
                         text_top=self.text_top, tilt_degrees=self.tilt_degrees,
                         needs_crop=self.needs_crop,
                         issues=[ChequeIssue(code=IssueCode.IMAGE_INCOMPLETE, detail=detail)])

    def _derive_issues(self, issues: dict, unclear: set) -> None:
        """Checks the backend decides from the values: words vs figures, missing amount, date."""
        # Words vs figures: decided by the numbers, not by the model's opinion
        if self.amount_in_words_value is not None and self.cheque_amount is not None:
            if abs(self.amount_in_words_value - self.cheque_amount) >= 0.01:
                words = (self.amount_in_words or "").strip().rstrip(".") \
                    or _fmt_amount(self.amount_in_words_value)
                issues[IssueCode.AMOUNT_MISMATCH] = ChequeIssue(
                    code=IssueCode.AMOUNT_MISMATCH,
                    detail=f"Amount in words ({words}) does not match "
                           f"figures ({_fmt_amount(self.cheque_amount)})",
                )
            else:
                issues.pop(IssueCode.AMOUNT_MISMATCH, None)

        if (self.cheque_amount is None or self.amount_in_words is None) and not (
                unclear & {UnclearField.CHEQUE_AMOUNT, UnclearField.AMOUNT_IN_WORDS}):
            issues.setdefault(IssueCode.AMOUNT_MISSING, ChequeIssue(code=IssueCode.AMOUNT_MISSING))

        cheque_date = parse_date(self.cheque_date)
        if cheque_date is None and UnclearField.CHEQUE_DATE not in unclear:
            issues.setdefault(IssueCode.DATE_ISSUE, ChequeIssue(code=IssueCode.DATE_ISSUE,
                                                                detail="date missing"))
        elif cheque_date and cheque_date < _months_before(now_uae().date(), STALE_AFTER_MONTHS):
            issues[IssueCode.DATE_ISSUE] = ChequeIssue(
                code=IssueCode.DATE_ISSUE,
                detail=f"dated {self.cheque_date}, more than {STALE_AFTER_MONTHS} months old",
            )


class ChequeExtraction(BaseModel):
    cheques: list[ChequeOcr]
