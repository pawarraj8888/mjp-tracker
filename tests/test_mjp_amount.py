"""Amount parsing for MJP documents: Devanagari numerals, lakh/crore words,
and the double-counting guard."""

from decimal import Decimal

import money


def test_devanagari_digits_normalized():
    assert money.normalize_digits("२४,९९,२५,०००") == "24,99,25,000"
    assert money.normalize_digits("रु. २८.७५") == "रु. 28.75"


def test_crore_word_scales():
    # 24.9925 कोटी == 24,99,25,000
    assert money.parse_indian_amount("24.9925 कोटी") == Decimal("249925000")
    assert money.parse_indian_amount("रु.24.9925 कोटी") == Decimal("249925000")


def test_lakh_word_and_default_unit():
    assert money.parse_indian_amount("28.75 लाख") == Decimal("2875000")
    # A GR cost column stated "in lakh" via default_unit.
    assert money.parse_indian_amount("171.81", default_unit="lakh") == \
        Decimal("17181000")


def test_full_rupees_not_rescaled_by_trailing_word():
    # Already written out in full: the trailing word must NOT multiply again.
    assert money.parse_indian_amount("24,99,25,000 कोटी") == Decimal("249925000")


def test_devanagari_with_crore():
    assert money.parse_indian_amount("२४.९९२५ कोटी") == Decimal("249925000")


def test_unknown_stays_none_not_zero():
    assert money.parse_indian_amount("") is None
    assert money.parse_indian_amount("not a number") is None
    assert money.parse_indian_amount(None) is None


def test_lakh_and_crore_conversion_math():
    assert money.parse_indian_amount("1 कोटी") == Decimal("10000000")
    assert money.parse_indian_amount("1 लाख") == Decimal("100000")
    assert money.parse_indian_amount("10 लक्ष") == Decimal("1000000")
