"""Match extracted cheques against expected rent payments.

A cheque matches when Amount and DueDate match, every optional field the caller sent
(ChequepayeeName, ChequeHolderName, ChequeHolderIBAN) matches, and the cheque itself has
no problems (alteration, words vs figures, stale date, missing signature, unclear fields, ...).
Fields the OCR marks as unclear are never compared; the caller is asked for a clear copy.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

from app.schemas import ChequeEntity, FileResult, RentPayment
from app.schemas.cheque import CLEAR_COPY, CORE_FIELDS, UnclearField
from app.utils.dates import parse_date

MATCHED = "Matched"
_AMOUNT_TOLERANCE = 0.01
_OVERLAP_NOTE = " Some cheques are overlapped and could not be fully read."


@dataclass
class _Candidate:
    url: str
    file_index: int
    cheque_index: int
    cheque: ChequeEntity
    used: bool = False

    @property
    def cheque_date(self) -> Optional[date]:
        return parse_date(self.cheque.cheque_date)

    def unclear(self, field: UnclearField) -> bool:
        return field in self.cheque.unclear_fields


def _same_amount(a: Optional[float], b: float) -> bool:
    return a is not None and abs(a - b) < _AMOUNT_TOLERANCE


def _fmt_amount(value: Optional[float]) -> str:
    if value is None:
        return "unreadable"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def _fmt_date(value: Optional[date]) -> str:
    return value.strftime("%d-%m-%Y") if value else "unreadable"


def _norm_name(value: str) -> str:
    """Strict: only case and repeated spaces are ignored."""
    return " ".join(value.split()).casefold()


def _norm_iban(value: str) -> str:
    return "".join(value.split()).upper()


# (request key, cheque field, label in messages, normaliser)
_TEXT_CHECKS = (
    ("ChequepayeeName", UnclearField.PAYEE_NAME, "Payee name", _norm_name),
    ("ChequeHolderName", UnclearField.HOLDER_NAME, "Holder name", _norm_name),
    ("ChequeHolderIBAN", UnclearField.IBAN, "IBAN", _norm_iban),
)


def _checks(payment: RentPayment, c: _Candidate) -> tuple[list[str], list[str], list[str]]:
    """(cheque issues, name / IBAN mismatches, unclear note) for this payment and cheque."""
    mismatches: list[str] = []
    extra_unclear: list[UnclearField] = []
    checks = () if c.cheque.unreadable else _TEXT_CHECKS  # never compare values from a bad image
    for key, field, label, norm in checks:
        expected = (getattr(payment, key) or "").strip()
        if not expected:
            continue
        if c.unclear(field):  # never compared; payee is already reported as a core field
            if field not in CORE_FIELDS:
                extra_unclear.append(field)
            continue
        actual = (getattr(c.cheque, field.value) or "").strip()
        if not actual:
            mismatches.append(f"{label} not found on the cheque.")
        elif norm(actual) != norm(expected):
            mismatches.append(f"{label} doesn't match. Cheque shows {actual}.")
    unclear = c.cheque.unclear(tuple(extra_unclear))
    return c.cheque.issue_messages(unclear), mismatches, c.cheque.unclear_messages(unclear)


def _other_issues(payment: RentPayment, c: _Candidate) -> list[str]:
    """Everything except amount / date; empty when the cheque is acceptable for this payment."""
    issues, mismatches, unclear = _checks(payment, c)
    return issues + mismatches + unclear


@dataclass
class Match:
    """A matched payment and where its cheque is: files[file_index].cheques[cheque_index]."""
    payment: RentPayment
    file_index: int
    cheque_index: int


def match_payments(payments: list[RentPayment], files: list[tuple[str, FileResult]]) -> list[Match]:
    """Fill MatchedWithOcr / MatchedOcrFileURL / OcrResponse on each payment, in place.

    Each cheque is used for at most one payment. CroppedOCRFileURL is left empty here;
    the caller crops only the returned matches.
    """
    candidates = [
        _Candidate(url, f, i, c)
        for f, (url, result) in enumerate(files)
        for i, c in enumerate(result.cheques)
    ]
    matches: list[Match] = []

    for payment in payments:
        due = payment.due_date
        hit = next(
            (c for c in candidates
             if not c.used and c.cheque_date == due
             and _same_amount(c.cheque.cheque_amount, payment.Amount)
             and not _other_issues(payment, c)),
            None,
        )
        if hit:
            hit.used = True
            payment.MatchedWithOcr = True
            payment.MatchedOcrFileURL = hit.url
            payment.OcrResponse = MATCHED
            matches.append(Match(payment, hit.file_index, hit.cheque_index))
        else:
            closest, message = _no_match(payment, due, candidates, files)
            payment.MatchedWithOcr = False
            payment.MatchedOcrFileURL = closest.url if closest else None
            payment.OcrResponse = message
    return matches


def _no_match(
    payment: RentPayment, due: date, candidates: list[_Candidate], files: list[tuple[str, FileResult]]
) -> tuple[Optional[_Candidate], str]:
    """Return the closest unused cheque (if any) and a message quoting what OCR read from it."""
    if not candidates:
        if files and all(r.status == "error" for _, r in files):
            return None, "None of the cheque files could be processed."
        return None, "No cheques found in the uploaded files."

    unused = [c for c in candidates if not c.used]
    if not unused:
        return None, "No matching cheque. All cheques are already matched to other payments."

    def date_gap(c: _Candidate) -> int:
        return abs((c.cheque_date - due).days) if c.cheque_date else 10**9

    def amount_gap(c: _Candidate) -> float:
        a = c.cheque.cheque_amount
        return abs(a - payment.Amount) if a is not None else float("inf")

    def issue_count(c: _Candidate) -> int:
        return len(_other_issues(payment, c))

    same_date = [c for c in unused if c.cheque_date == due]
    same_amount = [c for c in unused if _same_amount(c.cheque.cheque_amount, payment.Amount)]
    same_both = [c for c in same_amount if c.cheque_date == due]

    if same_both:
        c = min(same_both, key=issue_count)
    elif same_amount:
        c = min(same_amount, key=lambda c: (date_gap(c), issue_count(c)))
    elif same_date:
        c = min(same_date, key=lambda c: (amount_gap(c), issue_count(c)))
    else:
        c = min(unused, key=lambda c: (date_gap(c), amount_gap(c), issue_count(c)))

    message = _describe(payment, due, c)
    if any(other.cheque.error and "overlap" in other.cheque.error.lower() for other in unused):
        message += _OVERLAP_NOTE
    return c, message


def _describe(payment: RentPayment, due: date, c: _Candidate) -> str:
    """Why this cheque does not match the payment.

    Order: cheque issues (signature, words vs figures, damage first), amount / date,
    names / IBAN, then unclear fields. A missing or unclear amount / date is reported
    by the cheque's own issues instead. A blurred / incomplete / illegible cheque is not
    compared at all: only its problems and the clear-copy request are reported.
    """
    issues, mismatches, unclear = _checks(payment, c)
    compare = not c.cheque.unreadable
    amount_bad = (compare and c.cheque.cheque_amount is not None
                  and not _same_amount(c.cheque.cheque_amount, payment.Amount))
    date_bad = compare and c.cheque_date is not None and c.cheque_date != due

    parts = list(issues)
    if amount_bad and date_bad:
        parts.append(f"Amount and date don't match. Cheque shows "
                     f"{_fmt_amount(c.cheque.cheque_amount)} dated {_fmt_date(c.cheque_date)}.")
    elif amount_bad:
        parts.append(f"Amount doesn't match. Cheque amount is {_fmt_amount(c.cheque.cheque_amount)}.")
    elif date_bad:
        parts.append(f"Date doesn't match. Cheque date is {_fmt_date(c.cheque_date)}.")

    parts += mismatches + unclear
    return " ".join(parts) or f"Cheque details could not be verified. {CLEAR_COPY}"
