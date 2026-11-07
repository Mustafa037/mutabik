"""Spending category guess from the merchant name and the OCR text.

A deliberately simple, explainable keyword scorer (no model, no network): a
keyword found in the **merchant name** scores 3, one found anywhere in the OCR
text scores 1. The highest score wins; no match means ``OTHER``. The user can
always correct the category in the UI.
"""

import re
import unicodedata
from typing import Final, Optional

from app.schemas.receipt import Category

__all__ = ["categorize"]

MERCHANT_WEIGHT: Final[int] = 3
TEXT_WEIGHT: Final[int] = 1

#: Folded (accent-free, upper-case) keywords per category; matched as whole words.
_KEYWORDS: Final[dict[Category, tuple[str, ...]]] = {
    Category.GROCERY: (
        "MARKET", "MARKETI", "SUPERMARKET", "MIGROS", "BIM", "A101", "CARREFOUR", "BAKKAL", "MANAV", "KASAP",
        "GROCERY", "GROCERIES", "EKMEK", "SUT", "PEYNIR", "YUMURTA", "ZEYTIN", "DOMATES",
    ),
    Category.FOOD_DRINK: (
        "CAFE", "KAFE", "COFFEE", "KAHVE", "RESTAURANT", "RESTORAN", "LOKANTA", "PIDE", "KEBAP", "BURGER",
        "PIZZA", "BAKERY", "FIRIN", "PASTANE", "LATTE", "ESPRESSO", "MUFFIN", "CAY", "STARBUCKS", "DONER",
    ),
    Category.STATIONERY: ("KIRTASIYE", "STATIONERY", "DEFTER", "KALEM", "OFFICE"),
    Category.TRANSPORT: (
        "PETROL", "AKARYAKIT", "BENZIN", "DIZEL", "FUEL", "SHELL", "OPET", "TAKSI", "TAXI", "UBER", "OTOPARK",
        "PARKING", "METRO", "OTOBUS",
    ),
    Category.HEALTH: ("ECZANE", "PHARMACY", "HASTANE", "KLINIK", "CLINIC", "DOKTOR", "SAGLIK", "DENTAL"),
    Category.CLOTHING: ("GIYIM", "TEKSTIL", "AYAKKABI", "SHOES", "FASHION", "ZARA", "WAIKIKI"),
    Category.ELECTRONICS: ("ELEKTRONIK", "ELECTRONICS", "TEKNOSA", "VATAN", "BILGISAYAR", "COMPUTER", "MEDIAMARKT"),
}

STEM_MIN_LENGTH: Final[int] = 6  # longer keywords also match Turkish suffixes (ECZANE -> ECZANESI)


def _word_pattern(word: str) -> str:
    """Whole word for short keywords; word start for long ones (suffix tolerant)."""
    return rf"{word}\w*" if len(word) >= STEM_MIN_LENGTH else word


_PATTERNS: Final[dict[Category, re.Pattern[str]]] = {
    category: re.compile(r"\b(" + "|".join(_word_pattern(word) for word in words) + r")\b")
    for category, words in _KEYWORDS.items()
}


def _fold(text: str) -> str:
    """Upper-case ASCII form without accents (``Kırtasiye`` -> ``KIRTASIYE``)."""
    decomposed = unicodedata.normalize("NFKD", text.replace("ı", "i").replace("İ", "I"))
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn").upper()


def categorize(merchant_name: Optional[str], raw_text: Optional[str] = None) -> Category:
    """Pick the most likely category.

    Args:
        merchant_name: Merchant as extracted by the parser (may be ``None``).
        raw_text: Full OCR text (items such as ``EKMEK`` or ``LATTE`` help when the
            name is unknown).

    Returns:
        The best-scoring :class:`Category`, or ``Category.OTHER`` when nothing matches.
    """
    merchant = _fold(merchant_name or "")
    text = _fold(raw_text or "")
    scores = {
        category: MERCHANT_WEIGHT * len(pattern.findall(merchant)) + TEXT_WEIGHT * len(pattern.findall(text))
        for category, pattern in _PATTERNS.items()
    }
    best = max(scores, key=lambda category: scores[category])
    return best if scores[best] > 0 else Category.OTHER
