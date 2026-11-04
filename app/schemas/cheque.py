"""Cheque entity schema — also used as the OCR model's structured-output schema."""

from enum import Enum
import re
from typing import Annotated, Optional

from pydantic import BaseModel, Field, WithJsonSchema, field_validator, model_validator

from app.utils.amount_words import words_to_number
from app.utils.arabic import western_digits
from app.utils.dates import parse_date

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
    # Only part of the date is in doubt (e.g. year 2024 or 2026); the whole date is still dropped
    DATE_DAY = "date_day"
    DATE_MONTH = "date_month"
    DATE_YEAR = "date_year"


DATE_PARTS = (UnclearField.DATE_DAY, UnclearField.DATE_MONTH, UnclearField.DATE_YEAR)
DATE_FIELDS = {UnclearField.CHEQUE_DATE, *DATE_PARTS}

FIELD_LABELS = {
    UnclearField.CHEQUE_DATE: "Date",
    UnclearField.CHEQUE_AMOUNT: "Amount in figures",
    UnclearField.AMOUNT_IN_WORDS: "Amount in words",
    UnclearField.PAYEE_NAME: "Payee name",
    UnclearField.HOLDER_NAME: "Holder name",
    UnclearField.IBAN: "IBAN",
}

# Fields every cheque must have readable; holder name / IBAN only matter when the caller sends them
CORE_FIELDS = (UnclearField.CHEQUE_DATE, UnclearField.CHEQUE_AMOUNT,
               UnclearField.AMOUNT_IN_WORDS, UnclearField.PAYEE_NAME, *DATE_PARTS)


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
    IssueCode.PAYEE_ISSUE: "Payee name is missing",
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
    IssueCode.DATE_ISSUE: DATE_FIELDS,
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


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _labels(fields: list[UnclearField]) -> list[str]:
    """Field labels; doubtful date parts become one label: 'Day and year of the cheque date'."""
    fields = [UnclearField(f) for f in dict.fromkeys(fields)]
    if UnclearField.CHEQUE_DATE in fields:  # the whole date covers its parts
        fields = [f for f in fields if f not in DATE_PARTS]
    labels, parts_added = [], False
    for f in fields:
        if f not in DATE_PARTS:
            labels.append(FIELD_LABELS[f])
        elif not parts_added:
            parts = [p.value.removeprefix("date_") for p in DATE_PARTS if p in fields]
            labels.append(f"{_join(parts).capitalize()} of the cheque date")
            parts_added = True
    return labels


def unclear_message(fields: list[UnclearField]) -> str:
    """'Date and payee name are not clearly visible.' / 'Year of the cheque date is not clearly visible.'"""
    labels = _labels(fields)
    labels = [labels[0]] + [label if label == "IBAN" else label[0].lower() + label[1:]
                            for label in labels[1:]]
    return f"{_join(labels)} {'is' if len(labels) == 1 else 'are'} not clearly visible."


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

    def issue_messages(self, unclear: list[UnclearField], skip: frozenset = frozenset()) -> list[str]:
        """Issue messages, signature / words vs figures / damage first.

        A cut-off / blurred / illegible cheque gets one short message: everything else
        (missing signature, date, amount, ...) is only a consequence of the bad image.
        """
        if self.unreadable:
            return [UNREADABLE_MESSAGE]
        issues = [i for i in self.issues if i.code not in skip
                  and not _COVERED_BY_UNCLEAR.get(i.code, set()) & set(unclear)]
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


# The model sometimes repeats digits ("0000…") in numeric JSON fields until the output limit
# cuts the response. Asking for numbers as short strings avoids it; values are parsed back
# to numbers on validation, so the rest of the code (and the public API) still sees numbers.
# maxLength stops a runaway answer early (a loop used to burn ~6,000 tokens and 25 s per try)
_NUMBER_AS_TEXT = WithJsonSchema({"anyOf": [{"type": "string", "maxLength": 12}, {"type": "null"}]})
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _parse_number(value):
    """'6,667.00' / 'AED 6667' / '٦٬٢٥٠' / 6667 -> number; anything without a number -> None."""
    if value is None or isinstance(value, (int, float)):
        return value
    match = _NUMBER.search(western_digits(str(value)).replace(",", "").replace(" ", ""))
    return float(match.group()) if match else None


class TextTop(str, Enum):
    """Where the top of the printed text points. Easier for the model than a signed angle."""
    UP = "up"
    RIGHT = "right"
    DOWN = "down"
    LEFT = "left"


# Clockwise turn that brings the text upright
_QUARTER_TURN = {TextTop.UP: 0, TextTop.RIGHT: -90, TextTop.DOWN: 180, TextTop.LEFT: 90}

# The MICR line's centre must be at least this far from the cheque's centre (as a share of
# half the cheque's height / width) to tell which edge it is on
_MICR_MIN_OFFSET = 0.4


class ChequeOcr(ChequeEntity):
    """What the OCR model returns per cheque: the entity plus layout for crop / rotation.

    Layout fields are internal: responses are typed as ChequeEntity, so they are never
    serialised to clients.
    """

    # Numbers are requested as short strings (see _NUMBER_AS_TEXT) and parsed back below
    cheque_amount: Annotated[Optional[float], _NUMBER_AS_TEXT] = Field(
        None, description="Amount in figures, digits only, e.g. '6667' or '6667.50'"
    )
    installment_number: Annotated[Optional[int], _NUMBER_AS_TEXT] = Field(
        None, description="Installment number written on the cheque, e.g. '2'; null if none"
    )
    # English spelling of a name written in Arabic, so an English name in the request can be compared
    payee_name_latin: Optional[str] = Field(
        None, description="English spelling of payee_name if it is written in Arabic; else null"
    )
    holder_name_latin: Optional[str] = Field(
        None, description="English spelling of holder_name if it is written in Arabic; else null"
    )
    holder_name_ar: Optional[str] = Field(
        None, description="Account holder name as printed in Arabic; null if not printed in Arabic"
    )
    page: Annotated[Optional[int], _NUMBER_AS_TEXT] = Field(
        None, description="1-based page number ('1' for images)"
    )
    bounding_box: Optional[list[int]] = Field(
        None, description="[ymin, xmin, ymax, xmax] of the whole cheque, normalised 0-1000"
    )
    micr_box: Optional[list[int]] = Field(
        None, description="[ymin, xmin, ymax, xmax] of the MICR code line, normalised 0-1000; null if not visible"
    )
    text_top: Optional[TextTop] = Field(
        None, description="Direction the top of the cheque's printed text points in the image"
    )
    tilt_degrees: Annotated[Optional[float], _NUMBER_AS_TEXT] = Field(
        None, description="Small clockwise tilt (-45 to 45) still needed once the text points up"
    )
    needs_crop: Optional[bool] = Field(
        None, description="True if the image contains much more than this cheque"
    )

    @field_validator("cheque_amount", "tilt_degrees", mode="before")
    @classmethod
    def _parse_decimal(cls, v):
        return _parse_number(v)

    @field_validator("installment_number", "page", mode="before")
    @classmethod
    def _parse_whole(cls, v):
        number = _parse_number(v)
        return None if number is None else round(number)

    @property
    def micr_text_top(self) -> Optional[TextTop]:
        """Text direction from where the MICR line sits in the cheque box; None if unclear.

        The MICR line is always along the cheque's bottom edge, so the side of the box it is
        nearest to is the bottom, and the text top points the other way. Boxes are what the
        model reads most reliably; its own `text_top` mixes up left and right on sideways photos.
        """
        box, micr = self.bounding_box, self.micr_box
        if not (box and micr and len(box) == 4 and len(micr) == 4):
            return None
        half_h, half_w = (box[2] - box[0]) / 2, (box[3] - box[1]) / 2
        if half_h <= 0 or half_w <= 0 or micr[2] <= micr[0] or micr[3] <= micr[1]:
            return None  # empty or inverted box: not a real detection
        # MICR centre relative to the cheque centre, -1..1 on each axis
        dy = ((micr[0] + micr[2]) / 2 - (box[0] + box[2]) / 2) / half_h
        dx = ((micr[1] + micr[3]) / 2 - (box[1] + box[3]) / 2) / half_w
        if max(abs(dx), abs(dy)) < _MICR_MIN_OFFSET:
            return None  # near the middle: not a usable edge
        if abs(dy) >= abs(dx):
            return TextTop.UP if dy > 0 else TextTop.DOWN
        return TextTop.LEFT if dx > 0 else TextTop.RIGHT

    @property
    def orientation(self) -> Optional[TextTop]:
        """Where the top of the text points: from the MICR line when usable, else the model's text_top."""
        return self.micr_text_top or self.text_top

    @property
    def quarter_turn(self) -> int:
        """Clockwise quarter turn that makes the text upright (0, 90, 180 or -90)."""
        return _QUARTER_TURN.get(self.orientation, 0)

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
            setattr(self, "cheque_date" if field in DATE_PARTS else field.value, None)
            if field in (UnclearField.PAYEE_NAME, UnclearField.HOLDER_NAME):
                setattr(self, f"{field.value}_latin", None)
            if field == UnclearField.HOLDER_NAME:
                self.holder_name_ar = None

        if not issues.keys() & UNREADABLE_ISSUES:  # nothing is derived from a bad image
            self._derive_issues(issues, unclear)

        self.issues = list(issues.values())
        notes = ([self.error] if self.error else []) + self.problems()
        self.error = " ".join(notes) or None
        return self

    def as_unreadable(self, detail: str) -> "ChequeOcr":
        """Same position on the page, every value dropped: the image is too poor to trust any of it."""
        return ChequeOcr(page=self.page, bounding_box=self.bounding_box, micr_box=self.micr_box,
                         text_top=self.text_top, tilt_degrees=self.tilt_degrees,
                         needs_crop=self.needs_crop,
                         issues=[ChequeIssue(code=IssueCode.IMAGE_INCOMPLETE, detail=detail)])

    def _derive_issues(self, issues: dict, unclear: set) -> None:
        """Checks the backend decides from the values: words vs figures, missing amount, date."""
        # Words vs figures: the backend converts the words itself (the model only reads them)
        words_value = words_to_number(self.amount_in_words)
        if words_value is not None and self.cheque_amount is not None:
            if abs(words_value - self.cheque_amount) >= 0.01:
                words = self.amount_in_words.strip().rstrip(".")
                issues[IssueCode.AMOUNT_MISMATCH] = ChequeIssue(
                    code=IssueCode.AMOUNT_MISMATCH,
                    detail=f"Amount in words ({words}) does not match "
                           f"figures ({_fmt_amount(self.cheque_amount)})",
                )
            else:
                issues.pop(IssueCode.AMOUNT_MISMATCH, None)
        elif IssueCode.AMOUNT_MISMATCH in issues and self.amount_in_words and self.cheque_amount is not None:
            # Words the backend cannot convert (e.g. Arabic): keep the model's finding, same wording
            words = self.amount_in_words.strip().rstrip(".")
            issues[IssueCode.AMOUNT_MISMATCH] = ChequeIssue(
                code=IssueCode.AMOUNT_MISMATCH,
                detail=f"Amount in words ({words}) does not match "
                       f"figures ({_fmt_amount(self.cheque_amount)})",
            )

        if (self.cheque_amount is None or self.amount_in_words is None) and not (
                unclear & {UnclearField.CHEQUE_AMOUNT, UnclearField.AMOUNT_IN_WORDS}):
            issues.setdefault(IssueCode.AMOUNT_MISSING, ChequeIssue(code=IssueCode.AMOUNT_MISSING))

        # A readable date is judged by the DueDate comparison (wrong / old dates fail there);
        # only a cheque with no usable date at all is flagged here
        if parse_date(self.cheque_date) is not None:
            issues.pop(IssueCode.DATE_ISSUE, None)
        elif not unclear & DATE_FIELDS:
            issues.setdefault(IssueCode.DATE_ISSUE, ChequeIssue(code=IssueCode.DATE_ISSUE,
                                                                detail="date missing"))


class ChequeExtraction(BaseModel):
    cheques: list[ChequeOcr]
