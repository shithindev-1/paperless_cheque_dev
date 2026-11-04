"""Match extracted cheques against expected rent payments.

A cheque matches when Amount and DueDate match, every optional field the caller sent
(cheque_info: ChequepayeeName, ChequeHolderName, ChequeHolderNameAr, ChequeHolderIBAN) matches, and the cheque itself has
no problems (alteration, words vs figures, stale date, missing signature, unclear fields, ...).
Fields the OCR marks as unclear are never compared; the caller is asked for a clear copy.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Optional

from app.schemas import ChequeEntity, FileResult, RentPayment
from app.schemas.cheque import CLEAR_COPY, CORE_FIELDS, DATE_FIELDS, IssueCode, UnclearField
from app.utils.arabic import arabic_to_latin, western_digits
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

    @property
    def date_unclear(self) -> bool:
        return any(f in DATE_FIELDS for f in self.cheque.unclear_fields)


def _same_amount(a: Optional[float], b: float) -> bool:
    return a is not None and abs(a - b) < _AMOUNT_TOLERANCE


def _fmt_amount(value: Optional[float]) -> str:
    if value is None:
        return "unreadable"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def _fmt_date(value: Optional[date]) -> str:
    return value.strftime("%d-%m-%Y") if value else "unreadable"


def _norm_name(value: str) -> str:
    """Case, spaces and punctuation are ignored; every letter must match.

    'MS. SARA KHAN' == 'ms sara khan', 'ABC L.L.C.' == 'ABC LLC',
    'Al-Mansoori' == 'Al Mansoori'; 'Ahmad' != 'Ahmed'. Works for Arabic letters too.
    English vs Arabic (either way round) is compared by sound instead (_names_match).
    """
    return "".join(ch for ch in value.casefold() if ch.isalnum())


def _norm_iban(value: str) -> str:
    return "".join(western_digits(value).split()).upper()


def _is_latin(value: str) -> bool:
    return all(ch.isascii() for ch in unicodedata.normalize("NFKD", value) if ch.isalpha())


def _sound(value: str) -> str:
    """Consonant skeleton of a name in English letters: 'Mohammed Hamdi' == 'Mohamed Hamdy' == 'mhmdhmd'.

    Arabic script does not write short vowels, so English spellings of one Arabic name differ
    mostly in vowels (Ahmed / Ahmad, Omar / Umar), doubled letters (Mohammed / Mohamed) and a
    final h (Abdullah / Abdulla, Fatimah / Fatima).
    """
    text = unicodedata.normalize("NFKD", value.casefold())
    text = "".join(ch if ch.isascii() and ch.isalpha() else " " for ch in text)
    text = re.sub(r"h\b", "", text)
    for a, b in (("ph", "f"), ("ck", "k"), ("q", "k"), ("c", "k")):
        text = text.replace(a, b)
    text = re.sub(r"[aeiouyw\s]", "", text)
    return re.sub(r"(.)\1+", r"\1", text)


def _same_sound(a: str, b: str) -> bool:
    sa, sb = _sound(a), _sound(b)
    return bool(sa) and sa == sb


def _names_match(expected: str, shown: list[str], latin: str) -> bool:
    """Same script: every letter must match (_norm_name). Only when the cheque has no name in the
    sent script, English vs Arabic is compared by sound, using the OCR's English spelling of the
    Arabic name and the backend's own spelling of it."""
    english = _is_latin(expected)
    same_script = [s for s in shown if _is_latin(s) == english]
    if same_script:
        return any(_norm_name(s) == _norm_name(expected) for s in same_script)
    if english:  # English sent, only Arabic on the cheque
        return any(_same_sound(expected, s)
                   for actual in shown for s in (latin, arabic_to_latin(actual)) if s)
    return any(_same_sound(arabic_to_latin(expected), actual) for actual in shown)  # Arabic sent


def _shown_names(cheque: ChequeEntity, field: UnclearField) -> list[str]:
    """Every version of the name on the cheque: the holder may be printed in English and Arabic."""
    names = [getattr(cheque, field.value)]
    if field == UnclearField.HOLDER_NAME:
        names.append(getattr(cheque, "holder_name_ar", None))  # internal OCR field (ChequeOcr only)
    return [n.strip() for n in dict.fromkeys(names) if n and n.strip()]


# (request key, cheque field, label in messages); names may be sent in English or Arabic
_NAME_CHECKS = (
    ("ChequepayeeName", UnclearField.PAYEE_NAME, "Payee name"),
    ("ChequeHolderName", UnclearField.HOLDER_NAME, "Holder name"),
    ("ChequeHolderNameAr", UnclearField.HOLDER_NAME, "Arabic holder name"),
    ("ChequeHolderIBAN", UnclearField.IBAN, "IBAN"),
)


def _checks(payment: RentPayment, c: _Candidate) -> tuple[list[str], list[str], list[str]]:
    """(cheque issues, name / IBAN mismatches, unclear note) for this payment and cheque."""
    mismatches: list[str] = []
    extra_unclear: list[UnclearField] = []
    checks = () if c.cheque.unreadable else _NAME_CHECKS  # never compare values from a bad image
    for key, field, label in checks:
        expected = payment.expected(key)
        if not expected:
            continue
        if c.unclear(field):  # never compared; payee is already reported as a core field
            if field not in CORE_FIELDS and field not in extra_unclear:
                extra_unclear.append(field)
            continue
        shown = _shown_names(c.cheque, field)
        if not shown:
            mismatches.append(f"{label} not found on the cheque.")
            continue
        if field == UnclearField.IBAN:
            if _norm_iban(shown[0]) != _norm_iban(expected):
                mismatches.append(f"{label} doesn't match. Cheque shows {shown[0]}.")
            continue
        # Internal OCR field (ChequeOcr only): English spelling of an Arabic name
        latin = (getattr(c.cheque, f"{field.value}_latin", None) or "").strip()
        if not _names_match(expected, shown, latin):
            text = " / ".join(shown)
            if latin and _is_latin(expected) and not any(_is_latin(s) for s in shown):
                text += f" ({latin})"
            mismatches.append(f"{label} doesn't match. Cheque shows {text}.")
    unclear = c.cheque.unclear(tuple(extra_unclear))
    # A sent payee name is compared above, so the cheque's own "payee missing" would repeat it
    skip = frozenset({IssueCode.PAYEE_ISSUE}) if payment.expected("ChequepayeeName") else frozenset()
    return c.cheque.issue_messages(unclear, skip), mismatches, c.cheque.unclear_messages(unclear)


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

    # An unclear or missing value may be the right one, so it ranks before a clearly different
    # value (and the reply then reports that cheque's problem instead of blaming another one).
    # Order: same value, unclear, missing, different (nearest first).
    def date_gap(c: _Candidate) -> tuple[int, int]:
        if c.cheque_date:
            return (0, 0) if c.cheque_date == due else (3, abs((c.cheque_date - due).days))
        return (1, 0) if c.date_unclear else (2, 0)

    def amount_gap(c: _Candidate) -> tuple[int, float]:
        a = c.cheque.cheque_amount
        if a is not None:
            return (0, 0.0) if _same_amount(a, payment.Amount) else (3, abs(a - payment.Amount))
        return (1, 0.0) if c.unclear(UnclearField.CHEQUE_AMOUNT) else (2, 0.0)

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
