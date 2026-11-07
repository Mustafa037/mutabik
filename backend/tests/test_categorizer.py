"""Keyword categoriser."""

import pytest

from app.schemas.receipt import Category
from app.services.categorizer import categorize


@pytest.mark.parametrize(
    ("merchant", "text", "expected"),
    [
        ("ORNEK MARKET A.S", "", Category.GROCERY),
        ("MİGROS", "", Category.GROCERY),
        ("Yıldız Kırtasiye LTD. ŞTİ.", "", Category.STATIONERY),
        ("CORNER CAFE", "Latte x2", Category.FOOD_DRINK),
        ("Merkez Eczanesi", "", Category.HEALTH),
        ("OPET", "DIZEL 20 LT", Category.TRANSPORT),
        (None, "EKMEK 12,50\nSUT 1L", Category.GROCERY),  # items decide when the name is unknown
        ("ACME LTD", "TOPLAM 10,00", Category.OTHER),
        (None, None, Category.OTHER),
    ],
)
def test_categorize(merchant: str | None, text: str | None, expected: Category) -> None:
    assert categorize(merchant, text) is expected


def test_merchant_name_outweighs_item_words() -> None:
    assert categorize("CORNER CAFE", "EKMEK\nSUT") is Category.FOOD_DRINK
