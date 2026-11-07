"""Merchant key: spelling variants of one shop share a key."""

import pytest

from app.services.merchant import canonical_key, merchant_key


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ORNEK MARKET A.S", "ORNEK MARKET"),
        ("ORNEK MARKET A.S.", "ORNEK MARKET"),
        ("Örnek Market A.Ş.", "ORNEK MARKET"),
        ("YILDIZ KIRTASIYE LTD. STI.", "YILDIZ KIRTASIYE"),
        ("MIGROS TICARET A.S.", "MIGROS"),
        ("Migros", "MIGROS"),
        ("  corner   cafe ", "CORNER CAFE"),
        ("Walmart Inc.", "WALMART"),
        ("A.S.", "AS"),  # nothing but company words: keep them rather than lose the merchant
    ],
)
def test_merchant_key(name: str, expected: str) -> None:
    assert merchant_key(name) == expected


@pytest.mark.parametrize("name", [None, "", "   ", "***"])
def test_merchant_key_empty(name: str | None) -> None:
    assert merchant_key(name) is None


def test_variants_share_one_key() -> None:
    assert len({merchant_key(n) for n in ("ORNEK MARKET A.S", "ORNEK MARKET A.S.", "Örnek Market A.Ş.")}) == 1


def test_canonical_key_snaps_one_misread_letter() -> None:
    assert canonical_key("MIGR0S MARKET", ["MIGROS MARKET", "BIM"]) == "MIGROS MARKET"


def test_canonical_key_keeps_different_merchants_apart() -> None:
    assert canonical_key("STARBUCKS COFFEE", ["STARBURGER COFFEE"]) == "STARBUCKS COFFEE"
    assert canonical_key("MIGROS", ["MIGRAS"]) == "MIGROS"  # short names must match exactly


def test_canonical_key_exact_and_none() -> None:
    assert canonical_key("CORNER CAFE", ["CORNER CAFE"]) == "CORNER CAFE"
    assert canonical_key(None, ["X"]) is None
    assert canonical_key("NEW SHOP", []) == "NEW SHOP"
