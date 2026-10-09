"""Jalali month/weekday names and jstrftime_fa (NEW-10)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from paurubot.core.formatting import (
    JALALI_MONTHS,
    JALALI_WEEKDAYS,
    PERSIAN_DIGITS,
    gregorian_to_jalali,
    jalali_month_name,
    jalali_weekday,
    jstrftime_fa,
)

NOWRUZ_1405 = datetime(2026, 3, 21, 12, 0, tzinfo=UTC)  # a Saturday, midday UTC


def test_nowruz_1405_is_farvardin_first() -> None:
    assert gregorian_to_jalali(2026, 3, 21) == (1405, 1, 1)


def test_month_names_cover_farvardin_to_esfand() -> None:
    assert JALALI_MONTHS[0] == "Farvardin"
    assert JALALI_MONTHS[-1] == "Esfand"
    assert len(JALALI_MONTHS) == 12
    assert jalali_month_name(1) == "Farvardin"
    assert jalali_month_name(6) == "Shahrivar"
    assert jalali_month_name(12) == "Esfand"


@pytest.mark.parametrize("bad", [0, 13, -1, 99])
def test_month_name_rejects_out_of_range(bad: int) -> None:
    with pytest.raises(ValueError):
        jalali_month_name(bad)


def test_weekday_of_nowruz_is_shanbeh() -> None:
    assert NOWRUZ_1405.weekday() == 5  # the test date really is a Saturday
    assert jalali_weekday(NOWRUZ_1405) == "Shanbeh"
    assert "Shanbeh" in JALALI_WEEKDAYS and "Jomeh" in JALALI_WEEKDAYS
    assert len(JALALI_WEEKDAYS) == 7


def test_jstrftime_fa_words_and_persian_digits() -> None:
    text = jstrftime_fa(NOWRUZ_1405)
    assert "Shanbeh" in text and "Farvardin" in text
    assert "1405" not in text  # year is rendered in Persian digits
    assert "1".translate(PERSIAN_DIGITS) in text  # day 1
    assert "1405".translate(PERSIAN_DIGITS) in text  # year 1405
    assert set(text) & set("0123456789") == set()  # no Latin digits left


def test_jstrftime_fa_with_time_appends_clock() -> None:
    plain = jstrftime_fa(NOWRUZ_1405)
    timed = jstrftime_fa(NOWRUZ_1405, with_time=True)
    assert len(timed) > len(plain) and "Farvardin" in timed


def test_none_date_is_dash() -> None:
    assert jstrftime_fa(None) == "-"
    assert jalali_weekday(None) == "-"
