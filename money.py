"""Exact-decimal money parsing and formatting.

The procurement portals report amounts as free text like ``INR 4,291,550.826``
or ``Rs. 17,497,799.08``. The old parser stripped every non-digit, which
deleted the decimal point (``4,291,550.826`` -> ``4291550826``, a 1000x error).
Because the number of decimal places varies, no single divisor can undo it; the
amount has to be parsed properly.

This module is a dependency-free leaf: both ``tracker.py`` and the ``pipeline``
package import it, so there is exactly one implementation of the rules.

Design decisions
----------------
* Amounts are carried as :class:`decimal.Decimal` for exact arithmetic. They
  are rounded only for presentation.
* A missing / unparseable amount is ``None`` ("unknown"), never ``0`` -- zero
  is a real value (a nil award) and must not be conflated with "we don't know".
* On the wire (JSON) an amount is an exact decimal *string* (e.g.
  ``"4291550.826"``) or ``null``. JSON numbers are IEEE-754 doubles and would
  quietly lose the third decimal, so strings are the canonical serialization.
* Both Indian (``12,34,567.89``) and international (``1,234,567.89`` /
  ``1.234.567,89``) digit grouping are supported by treating the *last*
  separator as the decimal point.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

__all__ = [
    "parse_amount", "parse_currency", "dec_to_str", "str_to_dec",
    "add", "format_inr", "format_plain", "is_known",
    "normalize_digits", "parse_indian_amount",
]

# Devanagari (Marathi/Hindi) digits -> ASCII. Government resolutions mix these
# with Latin digits, so amounts must be normalized before parsing.
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Scale words. Crore = 10^7, lakh = 10^5. Both the correct Unicode spellings and
# the mangled forms produced by the GR portal's broken embedded-font ToUnicode
# map are listed, so "24.9925 कोटी" parses whether the PDF text layer is clean
# or garbled.
_CRORE_WORDS = ("कोटी", "कोटि", "करोड", "crore", "crores", " cr")
_LAKH_WORDS = ("लक्ष", "लाख", "lakh", "lakhs", "lac", "lacs")
_CRORE = Decimal(10) ** 7
_LAKH = Decimal(10) ** 5

# Currency tokens that may prefix/suffix an amount. Order matters only for
# detection, not stripping (all are removed before number parsing).
_CURRENCY_RE = re.compile(r"(?:inr|rs\.?|rupees?|₹|₹)", re.I)
# Everything that is not a digit, separator or sign is noise (currency words,
# NBSP \xa0, spaces, "/-", "only", etc.).
_NUMERIC_KEEP_RE = re.compile(r"[^0-9.,\-]")
_HAS_DIGIT_RE = re.compile(r"\d")


def parse_currency(text: str | None) -> str:
    """Return a normalized currency code for the amount text.

    Only INR is emitted today (the sources are Indian government portals); an
    unrecognized or absent currency yields ``""`` rather than a guess."""
    if not text:
        return ""
    return "INR" if _CURRENCY_RE.search(text) else ""


def parse_amount(text: str | None) -> Decimal | None:
    """Parse a money string into an exact :class:`~decimal.Decimal`.

    Returns ``None`` when the text contains no parseable number (unknown), so
    callers can distinguish "unknown" from a genuine zero. Never raises on
    malformed input.

    >>> parse_amount("INR\\xa04,291,550.826")
    Decimal('4291550.826')
    >>> parse_amount("Rs. 17,497,799.08")
    Decimal('17497799.08')
    >>> parse_amount("1.234.567,89")   # international
    Decimal('1234567.89')
    >>> parse_amount("") is None
    True
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    # Remove currency tokens first (``Rs.`` carries a dot that would otherwise
    # be read as a decimal point).
    stripped = _CURRENCY_RE.sub(" ", s).strip()
    # Negative when it leads with a minus or is wrapped in parentheses
    # (accounting style), not when a stray "-" appears in "/-".
    neg = stripped.startswith("-") or stripped.startswith("(")
    s = _NUMERIC_KEEP_RE.sub("", stripped)
    if not _HAS_DIGIT_RE.search(s):
        return None
    # A minus is a sign only at the front; strip stray separators or a
    # trailing minus (e.g. "1,234/-").
    s = s.strip(".,-").lstrip("+")
    if not s:
        return None

    has_dot = "." in s
    has_comma = "," in s
    if has_dot and has_comma:
        # The rightmost separator is the decimal point; the other is grouping.
        if s.rfind(".") > s.rfind(","):
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    elif has_comma:
        # Comma only. Treat as a decimal comma only when it looks like one
        # (a single comma with 1-2 trailing digits, e.g. European "12,50");
        # otherwise it is thousands grouping ("4,291,550").
        parts = s.split(",")
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            s = parts[0] + "." + parts[1]
        else:
            s = s.replace(",", "")
    # dot-only or plain digits: dot is already the decimal point.

    if not _HAS_DIGIT_RE.search(s):
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return -d if neg else d


def normalize_digits(text: str | None) -> str:
    """Translate Devanagari digits to ASCII (``२४`` -> ``24``). Non-digit
    characters are untouched. ``None`` becomes ``""``."""
    if not text:
        return ""
    return str(text).translate(_DEVANAGARI_DIGITS)


def parse_indian_amount(text: str | None,
                        default_unit: str | None = None) -> Decimal | None:
    """Parse an amount that may carry an Indian scale word or Devanagari digits.

    Handles ``"24.9925 कोटी"`` (-> 249925000), ``"28.75 लाख"`` (-> 2875000) and
    plain ``"रु. 24,99,25,000/-"`` (-> 24992500). ``default_unit`` (``"lakh"`` or
    ``"crore"``) applies when the text carries no scale word itself -- e.g. a GR
    cost table whose column header already states "रक्कम रु. लक्ष" (amount in
    lakh). Returns ``None`` for no parseable number, exactly like
    :func:`parse_amount`, so unknown is never confused with zero.

    The scale word is only honoured when the amount has no thousands grouping
    that already spells out full rupees: ``"24,99,25,000"`` is taken at face
    value even if the word "कोटी" trails it (the writer wrote out the rupees),
    whereas a bare ``"24.9925 कोटी"`` is scaled. This avoids the classic
    double-counting error (turning 24.99 crore, already written in full, into
    2.499 * 10^15).
    """
    if text is None:
        return None
    norm = normalize_digits(text)
    low = norm.lower()
    unit = None
    if any(w in norm or w in low for w in _CRORE_WORDS):
        unit = "crore"
    elif any(w in norm or w in low for w in _LAKH_WORDS):
        unit = "lakh"
    base = parse_amount(norm)
    if base is None:
        return None
    # A value already written out in full rupees (>= 1 lakh with grouping, or a
    # large integer) is not re-scaled by a trailing word.
    written_in_full = base >= _LAKH
    if unit is None:
        unit = default_unit
    if unit == "crore" and not written_in_full:
        return _drop_trailing_zeros(base * _CRORE)
    if unit == "lakh" and not written_in_full:
        return _drop_trailing_zeros(base * _LAKH)
    return base


def _drop_trailing_zeros(d: Decimal) -> Decimal:
    """Scaling a decimal by 10^5/10^7 leaves trailing fractional zeros
    (``24.9925 * 10^7`` -> ``249925000.0000``). Collapse a whole result back to
    an integer so the canonical string is ``"249925000"``, not ``"...0000"``."""
    integral = d.to_integral_value()
    return integral if d == integral else d


def str_to_dec(value: str | int | float | Decimal | None) -> Decimal | None:
    """Coerce a stored canonical value (decimal string, number, or ``None``)
    back to a Decimal. Unknown stays ``None``."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def dec_to_str(d: Decimal | None) -> str | None:
    """Canonical JSON serialization: an exact decimal string, or ``None``.

    Uses a plain (non-scientific) representation so large amounts never turn
    into ``1E+7`` style text."""
    if d is None:
        return None
    # Normalize away exponent notation while preserving all significant digits.
    sign, digits, exp = d.as_tuple()
    if exp >= 0:
        s = str(int(d))
    else:
        s = format(d, "f")
    return s


def is_known(value) -> bool:
    """True when an amount is present (not unknown)."""
    return str_to_dec(value) is not None


def add(values) -> Decimal:
    """Exact sum of an iterable of amounts (strings/Decimals/None). Unknown
    values are skipped; the result is always a Decimal (0 for an empty set)."""
    total = Decimal(0)
    for v in values:
        d = v if isinstance(v, Decimal) else str_to_dec(v)
        if d is not None:
            total += d
    return total


def _indian_group(int_digits: str) -> str:
    """Group an integer digit string in the Indian system: the last three
    digits, then pairs (12,34,567)."""
    if len(int_digits) <= 3:
        return int_digits
    head, tail = int_digits[:-3], int_digits[-3:]
    head = re.sub(r"(?<=\d)(?=(\d\d)+$)", ",", head)
    return head + "," + tail


def format_plain(value, decimals: int | None = None) -> str:
    """Indian-grouped plain number for presentation (e.g. ``42,91,550.83``).

    ``decimals=None`` keeps the amount's own scale (trailing zeros trimmed).
    Rounding is presentation-only; the stored value is untouched."""
    d = value if isinstance(value, Decimal) else str_to_dec(value)
    if d is None:
        return "Unknown"
    neg = d < 0
    d = abs(d)
    if decimals is not None:
        q = Decimal(1).scaleb(-decimals)
        d = d.quantize(q)
    int_part = int(d)
    frac = d - int_part
    out = _indian_group(str(int_part))
    if frac != 0:
        frac_str = format(frac, "f")[2:].rstrip("0")
        if frac_str:
            out += "." + frac_str
    elif decimals:
        out += "." + "0" * decimals
    return ("-" if neg else "") + out


def format_inr(value) -> str:
    """Compact INR presentation: ``1.51 Cr``, ``45 L``, or grouped rupees.

    Rounds only for display. Unknown -> ``""`` (callers decide how to show it)."""
    d = value if isinstance(value, Decimal) else str_to_dec(value)
    if d is None:
        return ""
    neg = d < 0
    d = abs(d)
    crore = Decimal(10) ** 7
    lakh = Decimal(10) ** 5
    if d >= crore:
        s = (d / crore).quantize(Decimal("0.01"))
        out = _trim(s) + " Cr"
    elif d >= lakh:
        s = (d / lakh).quantize(Decimal("0.01"))
        out = _trim(s) + " L"
    else:
        out = _indian_group(str(int(d.quantize(Decimal(1)))))
    return ("-" if neg else "") + out


def _trim(d: Decimal) -> str:
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s
