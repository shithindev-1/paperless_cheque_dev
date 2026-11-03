"""Amount in words -> number, for the words-vs-figures check (no I/O).

Handles cheque wording such as "Five Thousand Three Hundred Thirty Three Only",
"AED Ten Thousand Dirhams only.", "Six thousand and fifty fils", "One Lakh Twenty Thousand",
hyphens ("Sixty-Seven"), "50/100" fils, and small handwriting / OCR misspellings
("Thousant", "Hundered"). Returns None when the words cannot be read with confidence, so the
check is skipped rather than guessed.
"""

import difflib
import re
from typing import Optional

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES = {"thousand": 1_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000,
           "million": 1_000_000, "crore": 10_000_000, "billion": 1_000_000_000}
_CURRENCY = {"dirham", "dirhams", "aed", "dhs", "dh", "uae"}
_FILS = {"fils", "fil", "fills"}
_FILLER = {"and", "only", "of", "the", "rupees", "amount"}
_KNOWN = list(_UNITS) + list(_SCALES) + ["hundred"] + list(_CURRENCY | _FILS | _FILLER)


def _word(token: str) -> Optional[str]:
    """The known word this token is, allowing small misspellings; None if unrecognised."""
    if token in _KNOWN:
        return token
    close = difflib.get_close_matches(token, _KNOWN, n=1, cutoff=0.8) if len(token) > 3 else []
    return close[0] if close else None


def words_to_number(text: Optional[str]) -> Optional[float]:
    if not text or not text.strip():
        return None
    lowered = re.sub(r"(?<=\d),(?=\d)", "", text.lower())  # "5,000" -> "5000"
    fraction = re.search(r"(\d{1,2})\s*/\s*100", lowered)  # "and 50/100"
    tokens = re.findall(r"[a-z]+|\d+(?:\.\d+)?", re.sub(r"\d{1,2}\s*/\s*100", " ", lowered))

    total = current = 0.0
    at_and: Optional[float] = None  # running value at the last "and" (dirhams / fils split)
    seen_number = False
    dirhams: Optional[float] = None
    fils: Optional[float] = float(fraction.group(1)) if fraction else None

    for token in tokens:
        if token[0].isdigit():
            current += float(token)
            seen_number = True
            continue
        word = _word(token)
        if word is None:
            return None  # an unreadable word: do not guess the amount
        if word in _UNITS:
            current += _UNITS[word]
            seen_number = True
        elif word == "hundred":
            current = max(current, 1) * 100
            seen_number = True
        elif word in _SCALES:
            total += max(current, 1) * _SCALES[word]
            current = 0
            seen_number = True
        elif word in _CURRENCY:
            if total or current:
                dirhams, total, current = total + current, 0.0, 0.0
        elif word in _FILS:
            value = total + current
            if dirhams is None:  # "Six thousand and fifty fils": split at the last "and"
                split = at_and if at_and is not None else value - current % 100
                dirhams, value = split, value - split
            fils, total, current = value, 0.0, 0.0
        elif word == "and":
            at_and = total + current
    if not seen_number:
        return None
    if dirhams is None:
        dirhams = total + current
    elif total or current:  # "Dirhams Five Thousand": currency word came first
        dirhams += total + current
    return round(dirhams + (fils or 0) / 100, 2)
