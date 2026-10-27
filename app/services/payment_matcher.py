"""Match extracted cheques against expected rent payments (Amount + DueDate)."""

from dataclasses import dataclass
from datetime import date
from typing import Optional

from app.schemas import ChequeEntity, FileResult, RentPayment
from app.utils.dates import parse_date

MATCHED = "Matched"
_AMOUNT_TOLERANCE = 0.01
_OVERLAP_NOTE = " Some cheques are overlapped and could not be fully read."


@dataclass
class _Candidate:
    url: str
    cheque: ChequeEntity
    used: bool = False

    @property
    def cheque_date(self) -> Optional[date]:
        return parse_date(self.cheque.cheque_date)


def _same_amount(a: Optional[float], b: float) -> bool:
    return a is not None and abs(a - b) < _AMOUNT_TOLERANCE


def _fmt_amount(value: Optional[float]) -> str:
    if value is None:
        return "unreadable"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def _fmt_date(value: Optional[date]) -> str:
    return value.strftime("%d-%m-%Y") if value else "unreadable"


def match_payments(payments: list[RentPayment], files: list[tuple[str, FileResult]]) -> None:
    """Fill MatchedWithOcr / MatchedOcrFile / OcrResponse on each payment, in place.

    A cheque matches a payment when its amount and date equal Amount and DueDate.
    Each cheque is used for at most one payment.
    """
    candidates = [_Candidate(url, c) for url, result in files for c in result.cheques]

    for payment in payments:
        due = payment.due_date
        hit = next(
            (c for c in candidates
             if not c.used and c.cheque_date == due and _same_amount(c.cheque.cheque_amount, payment.Amount)),
            None,
        )
        if hit:
            hit.used = True
            payment.MatchedWithOcr = True
            payment.MatchedOcrFile = hit.url
            payment.OcrResponse = MATCHED
        else:
            closest, message = _no_match(payment, due, candidates, files)
            payment.MatchedWithOcr = False
            payment.MatchedOcrFile = closest.url if closest else None
            payment.OcrResponse = message


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

    same_date = [c for c in unused if c.cheque_date == due]
    same_amount = [c for c in unused if _same_amount(c.cheque.cheque_amount, payment.Amount)]

    if same_amount:
        c = min(same_amount, key=date_gap)
        message = f"Date doesn't match. Cheque date is {_fmt_date(c.cheque_date)}."
    elif same_date:
        c = min(same_date, key=amount_gap)
        message = f"Amount doesn't match. Cheque amount is {_fmt_amount(c.cheque.cheque_amount)}."
    else:
        c = min(unused, key=lambda c: (date_gap(c), amount_gap(c)))
        message = (f"Amount and date don't match. Cheque shows "
                   f"{_fmt_amount(c.cheque.cheque_amount)} dated {_fmt_date(c.cheque_date)}.")

    if any(other.cheque.error for other in unused):
        message += _OVERLAP_NOTE
    return c, message
