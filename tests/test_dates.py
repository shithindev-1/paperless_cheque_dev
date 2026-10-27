from datetime import date

import pytest

from app.utils.dates import parse_date

NOV_5 = date(2026, 11, 5)


@pytest.mark.parametrize("text", [
    "2026-11-05",
    "2026-11-05T00:00:00",
    "2026-11-05T00:00:00Z",
    "20261105",
    "05/11/2026",
    "05-11-2026",
    "05.11.2026",
    "5/11/26",
    "2026/11/05",
    "5 Nov 2026",
    "5-November-2026",
    "Nov 5, 2026",
    "5th Nov 2026",
    "05/11/2026 00:00:00",
    "05/11/2026 12:00 AM",
])
def test_parses_common_formats(text):
    assert parse_date(text) == NOV_5


def test_ambiguous_numeric_date_is_day_first():
    assert parse_date("05/11/2026") == date(2026, 11, 5)


def test_month_first_used_only_when_day_first_impossible():
    assert parse_date("11/25/2026") == date(2026, 11, 25)


def test_dotnet_json_date():
    assert parse_date("/Date(1793836800000)/") == NOV_5


@pytest.mark.parametrize("text", [None, "", "not a date", "31-02-2026"])
def test_unreadable_returns_none(text):
    assert parse_date(text) is None
