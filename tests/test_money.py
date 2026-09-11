"""Tests for the exact-decimal money parser (money.py).

These pin the bug the whole audit turned on: the old parser deleted the decimal
point (INR 4,291,550.826 -> 4291550826). The parser must preserve decimals,
handle Indian and international grouping, and treat a missing amount as unknown
(None), never zero.
"""

from decimal import Decimal

import money
import pytest


@pytest.mark.parametrize("raw,expected", [
    # The three discrepancies named in the brief.
    ("INR\xa04,291,550.826", "4291550.826"),
    ("INR\xa017,497,799.08", "17497799.08"),
    ("INR\xa02,252,666.17", "2252666.17"),
    # Currency prefixes with a dot must not be read as a decimal point.
    ("Rs. 17,497,799.08", "17497799.08"),
    ("Rs.45,00,000/-", "4500000"),
    ("₹ 5,00,000", "500000"),
    # Indian vs international grouping resolve to the same number.
    ("12,34,567.89", "1234567.89"),
    ("1,234,567.89", "1234567.89"),
    ("1.234.567,89", "1234567.89"),   # European
    ("12,50", "12.50"),               # European decimal comma
    # Signs and accounting negatives.
    ("-4,500.50", "-4500.50"),
    ("(1,200.00)", "-1200.00"),
    # Zero is a real value, distinct from unknown.
    ("0", "0"),
    ("INR 0.00", "0.00"),
])
def test_parse_amount_exact(raw, expected):
    assert money.dec_to_str(money.parse_amount(raw)) == expected


@pytest.mark.parametrize("raw", ["", None, "N/A", "Not Available", "-", "  "])
def test_missing_is_unknown_not_zero(raw):
    assert money.parse_amount(raw) is None
    assert money.is_known(money.parse_amount(raw)) is False


def test_no_uniform_divisor_would_fix_all():
    """The corrupted values differ by decimal-place count, so dividing every
    record by one factor cannot repair them -- the parser must be decimal-aware.
    """
    a = money.parse_amount("INR 4,291,550.826")   # 3 decimals
    b = money.parse_amount("INR 17,497,799.08")   # 2 decimals
    assert a == Decimal("4291550.826")
    assert b == Decimal("17497799.08")
    # The values the OLD parser stored (decimal point deleted).
    corrupted_a = Decimal("4291550826")
    corrupted_b = Decimal("1749779908")
    # a needs /1000, b needs /100 -- different divisors.
    assert corrupted_a / 1000 == a
    assert corrupted_b / 100 == b
    # So the divisor that repairs `a` (1000) does NOT repair `b`.
    assert corrupted_b / 1000 != b


def test_add_skips_unknown_and_is_exact():
    total = money.add(["4291550.826", "17497799.08", None, "2252666.17"])
    assert total == Decimal("24042016.076")


def test_format_inr_presentation_only():
    assert money.format_inr("17497799.08") == "1.75 Cr"
    assert money.format_inr("4291550.826") == "42.92 L"
    assert money.format_inr("59000") == "59,000"
    assert money.format_inr(None) == ""       # unknown shows as blank, not 0
    assert money.format_inr("0") == "0"


def test_indian_grouping():
    assert money.format_plain("12345678") == "1,23,45,678"
    assert money.format_plain("100") == "100"
