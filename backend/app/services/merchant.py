"""Merchant identity from the printed name (no tax number needed).

OCR reads the same shop slightly differently every time (``ORNEK MARKET A.S`` /
``Örnek Market A.Ş.`` / ``0RNEK MARKET``). To group receipts by merchant, a name is
reduced to a *key*:

1. accent-free upper case, dots removed (``A.Ş.`` -> ``AS``), punctuation -> spaces;
2. company-type words dropped (``A.Ş.``, ``LTD``, ``ŞTİ``, ``TİCARET``, ``SANAYİ``...);
3. tokens joined by single spaces.

:func:`canonical_key` then snaps a new key to an already known one when the two are
almost identical (one misread letter), so OCR noise does not create a new merchant.
The user can still merge two merchants by hand (``POST /merchants/merge``).
"""

import difflib
import re
import unicodedata
from collections.abc import Iterable
from typing import Final, Optional

__all__ = ["merchant_key", "canonical_key"]

#: Words that describe the company form, not the shop (folded, dots removed).
_LEGAL_WORDS: Final[frozenset[str]] = frozenset(
    {
        "AS", "ANONIM", "SIRKETI", "LTD", "LIMITED", "STI", "SAN", "SANAYI", "TIC", "TICARET", "PAZ", "PAZARLAMA",
        "INC", "LLC", "CORP", "CO", "GMBH", "PLC",
    }
)
#: Keys at least this similar (``difflib`` ratio) are treated as one merchant.
FUZZY_CUTOFF: Final[float] = 0.9
FUZZY_MIN_LENGTH: Final[int] = 6  # short names (BIM, A101, SOK) must match exactly
MAX_KEY_LENGTH: Final[int] = 255


def _fold(text: str) -> str:
    """Upper-case ASCII form without accents (``Şişe`` -> ``SISE``, ``İ``/``ı`` -> ``I``)."""
    decomposed = unicodedata.normalize("NFKD", text.replace("ı", "i").replace("İ", "I"))
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn").upper()


def merchant_key(name: Optional[str]) -> Optional[str]:
    """Normalised identity of a merchant name.

    Args:
        name: Merchant name as read from the receipt (may be ``None``).

    Returns:
        The key, or ``None`` for an empty / meaningless name. A name made only of
        company-type words keeps them (so it still gets a key).
    """
    if not name:
        return None
    folded = _fold(name).replace(".", "")
    tokens = re.sub(r"[^A-Z0-9]+", " ", folded).split()
    kept = [token for token in tokens if token not in _LEGAL_WORDS] or tokens
    key = " ".join(kept)[:MAX_KEY_LENGTH]
    return key or None


def canonical_key(key: Optional[str], known_keys: Iterable[str]) -> Optional[str]:
    """Snap ``key`` to the closest known key when they differ by OCR noise only.

    Args:
        key: Freshly computed key.
        known_keys: Keys already stored.

    Returns:
        A known key when it is identical or at least ``FUZZY_CUTOFF`` similar (and the
        name is long enough for a fuzzy match to be safe), otherwise ``key`` itself.
    """
    if key is None:
        return None
    known = [candidate for candidate in known_keys if candidate]
    if key in known:
        return key
    if len(key) < FUZZY_MIN_LENGTH:
        return key
    close = difflib.get_close_matches(key, [c for c in known if len(c) >= FUZZY_MIN_LENGTH], n=1, cutoff=FUZZY_CUTOFF)
    return close[0] if close else key
