"""Text normalisation, fuzzy matching and funding classification.

Pure functions, no I/O. rapidfuzz is used when available with a stdlib
(difflib) fallback so the module works and the tests run without it.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

try:  # optional dependency, per docs/ASSUMPTIONS.md
    from rapidfuzz import fuzz as _rf_fuzz
except Exception:  # pragma: no cover - exercised only when rapidfuzz absent
    _rf_fuzz = None

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(pvt|private|ltd|limited|llp|company|co|corporation|corp|inc|"
    r"and co|& co|jv|j\.?v\.?)\b", re.I)
_LEAD_HONORIFIC_RE = re.compile(
    r"^(m\s*/?\s*s\.?|messrs\.?|shri\.?|sri\.?|smt\.?|mr\.?|mrs\.?)\s+", re.I)
_PUNCT_RE = re.compile(r"[.,&'\"()/\\:;_|-]+")
_WS_RE = re.compile(r"\s+")


def clean(text: str | None) -> str:
    """Collapse whitespace and strip a few punctuation variants (no em dash)."""
    if not text:
        return ""
    for src, dst in (("—", "-"), ("–", "-"), ("−", "-"),
                     (" ", " ")):
        text = text.replace(src, dst)
    return _WS_RE.sub(" ", text).strip()


def normalize_org(name: str | None) -> str:
    """Lowercase, punctuation-stripped org name for dedupe comparison."""
    s = _PUNCT_RE.sub(" ", clean(name).casefold())
    return _WS_RE.sub(" ", s).strip()


def normalize_name(name: str | None) -> str:
    """Contractor name folded to a comparable key: drop honorifics, legal
    suffixes and punctuation, lowercase, collapse whitespace."""
    s = clean(name)
    s = _LEAD_HONORIFIC_RE.sub("", s)
    s = _PUNCT_RE.sub(" ", s.casefold())
    s = _LEGAL_SUFFIX_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def tokens(text: str) -> set[str]:
    # \w with re.UNICODE keeps Devanagari letters, so Marathi/Hindi names are
    # not silently reduced to an empty token set.
    return set(t for t in re.findall(r"\w+", (text or "").casefold(), re.UNICODE)
               if t)


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() * 100.0


def token_set_ratio(a: str, b: str) -> float:
    """0..100 similarity, order-independent. rapidfuzz when present, else a
    stdlib implementation of the same token-set algorithm."""
    a, b = (a or ""), (b or "")
    if _rf_fuzz is not None:
        return float(_rf_fuzz.token_set_ratio(a, b))
    ta, tb = tokens(a), tokens(b)
    if not ta and not tb:
        # Only identical (or identically-empty) inputs are a perfect match;
        # two different strings that both tokenised to nothing are NOT.
        return 100.0 if a.strip().casefold() == b.strip().casefold() else 0.0
    inter = " ".join(sorted(ta & tb))
    sa = (inter + " " + " ".join(sorted(ta - tb))).strip()
    sb = (inter + " " + " ".join(sorted(tb - ta))).strip()
    return max(_ratio(inter, sa), _ratio(inter, sb), _ratio(sa, sb))


# --- funding classification -------------------------------------------------

_SCHEMES = [
    ("Jal Jeevan Mission", r"\bjjm\b|jal jeevan"),
    ("PMGSY", r"\bpmgsy\b|pradhan mantri gram sadak"),
    ("AMRUT", r"\bamrut\b"),
    ("Nagarothan", r"nagarothan|nagrotthan"),
    ("Amdar Nidhi / MLA fund", r"amdar|aamdar|\bmla\b fund|mla local|आमदार"),
    ("Khasdar / MP fund", r"khasdar|\bmp\b fund|mplad|खासदार"),
    ("DPDC / District Planning", r"\bdpdc\b|d\.p\.d\.c|district planning|"
                                 r"जिल्हा नियोजन|niyojan"),
    ("Dalit Vasti", r"dalit\s*vasti|dalitvasti|dalit wasti"),
    ("15th Finance Commission", r"15th finance|vitta ayog|vitt ayog"),
    ("MGNREGA", r"nrega|mgnrega|rojgar hami"),
]
_CENTRAL = r"nhai|morth|cpwd|\baai\b|jnpa|railway|central|world bank|\badb\b"


def funding_scheme(text: str | None) -> str:
    low = (text or "").casefold()
    for name, pat in _SCHEMES:
        if re.search(pat, low):
            return name
    return ""


def funding_level(text: str | None, portal: str = "") -> str:
    low = ((text or "") + " " + (portal or "")).casefold()
    if re.search(_CENTRAL, low):
        return "central"
    if re.search(r"municipal|corporation|nagar palika|zilla parishad|"
                 r"gram panchayat|\bzp\b", low):
        return "local"
    return "state"


def funding_source(text: str | None) -> str:
    low = (text or "").casefold()
    if "world bank" in low:
        return "world_bank"
    if re.search(r"\badb\b|asian development", low):
        return "adb"
    if funding_scheme(text):
        return "css"  # centrally sponsored scheme
    return "unknown"
