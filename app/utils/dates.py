"""Lenient date parsing for cheque dates and payment due dates."""

import re
from datetime import date, datetime, timezone
from typing import Optional

# Tried in order. Day-first (UAE) wins for ambiguous dates like 05/11/2026;
# month-first is only used when day-first is impossible (e.g. 11/25/2026).
_FORMATS = (
    # numeric, day first
    "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d %m %Y",
    "%d-%m-%y", "%d/%m/%y", "%d.%m.%y",
    # numeric, year first
    "%Y/%m/%d", "%Y.%m.%d", "%Y %m %d",
    # month names: 5 Nov 2026, 5-November-2026, Nov 5 2026
    "%d %b %Y", "%d-%b-%Y", "%d %B %Y", "%d-%B-%Y", "%d %b %y", "%d-%b-%y",
    "%b %d %Y", "%B %d %Y", "%b-%d-%Y", "%B-%d-%Y",
    "%Y-%b-%d", "%Y %b %d",
    # numeric, month first (fallback)
    "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y",
)
_DOTNET_DATE = re.compile(r"^/Date\((-?\d+)")                 # /Date(1730764800000)/
_TRAILING_TIME = re.compile(r"[ T]\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?\s*([AaPp][Mm])?\s*(Z|[+-]\d{2}:?\d{2})?$")
_ORDINAL = re.compile(r"(\d)(st|nd|rd|th)\b", re.IGNORECASE)  # 5th Nov -> 5 Nov


def parse_date(value) -> Optional[date]:
    """Parse a date written in almost any common format. Returns None if it can't."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        return None

    if m := _DOTNET_DATE.match(text):
        return datetime.fromtimestamp(int(m.group(1)) / 1000, timezone.utc).date()

    # ISO: 2026-11-05, 2026-11-05T00:00:00, 2026-11-05T00:00:00Z, 20261105
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass

    text = _TRAILING_TIME.sub("", text)
    text = _ORDINAL.sub(r"\1", text)
    text = re.sub(r"\s+", " ", text.replace(",", " ")).strip()

    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
