"""Reconciler: subtotal + tax == total within a tolerance."""

from decimal import Decimal

import pytest

from app.schemas.receipt import ReconciliationStatus
from app.services.reconciler import reconcile

VERIFIED = ReconciliationStatus.VERIFIED
FLAGGED = ReconciliationStatus.FLAGGED_FOR_REVIEW


def test_exact_match_is_verified() -> None:
    result = reconcile("96.80", "9.68", "106.48")
    assert result.status is VERIFIED
    assert result.expected_total == Decimal("106.48")
    assert result.difference == Decimal("0.00")


@pytest.mark.parametrize("total", ["106.50", "106.46"])
def test_difference_within_tolerance_is_verified(total: str) -> None:
    assert reconcile("96.80", "9.68", total).status is VERIFIED


def test_difference_above_tolerance_is_flagged() -> None:
    result = reconcile("100.00", "20.00", "130.00")
    assert result.status is FLAGGED
    assert result.difference == Decimal("10.00")
    assert "uyuşmuyor" in result.message


@pytest.mark.parametrize("args", [(None, None, "2.00"), ("1.00", None, "2.00"), ("1.00", "1.00", None), (None, "1.00", None)])
def test_missing_amount_is_flagged(args: tuple[str | None, ...]) -> None:
    result = reconcile(*args)
    assert result.status is FLAGGED
    assert "okunamadı" in result.message


def test_mixed_signs_are_flagged() -> None:
    assert reconcile("100.00", "-20.00", "80.00").status is FLAGGED


def test_decimal_arithmetic_has_no_float_noise() -> None:
    assert reconcile("0.10", "0.20", "0.30", tolerance="0").status is VERIFIED


def test_custom_tolerance() -> None:
    assert reconcile("10.00", "1.00", "11.30", tolerance="0.50").status is VERIFIED


@pytest.mark.parametrize("bad", ["abc", float("nan"), True])
def test_invalid_amount_raises(bad: object) -> None:
    with pytest.raises(ValueError):
        reconcile(bad, "1.00", "2.00")  # type: ignore[arg-type]


def test_negative_tolerance_raises() -> None:
    with pytest.raises(ValueError):
        reconcile("1.00", "1.00", "2.00", tolerance="-1")


# ---------------------------------------------------------------------------
# How the receipt was verified (method)
# ---------------------------------------------------------------------------
def test_printed_subtotal_has_method_printed() -> None:
    assert reconcile("96.80", "9.68", "106.48").method == "printed"


def test_derived_and_manual_methods_are_kept() -> None:
    assert reconcile("96.80", "9.68", "106.48", subtotal_method="derived_from_rate").method == "derived_from_rate"
    assert reconcile("96.80", "9.68", "106.48", subtotal_method="manual").method == "manual"


def test_unverified_receipt_has_no_method() -> None:
    assert reconcile("100.00", "20.00", "130.00").method is None
    assert reconcile(None, None, "10.00").method is None


def test_implied_single_rate_is_verified_with_derived_subtotal() -> None:
    result = reconcile(None, "9.68", "106.48", tax_rates=[10])
    assert result.status is VERIFIED and result.method == "implied_rate"
    assert result.subtotal_amount == Decimal("96.80")


def test_implied_mixed_rates_use_the_blended_rate() -> None:
    """The real receipt: 1 % and 20 % items, TOPKDV 46,60, TOPLAM 427,42 (blended 12.2 %)."""
    result = reconcile(None, "46.60", "427.42", tax_rates=[1, 20])
    assert result.status is VERIFIED and result.method == "implied_rate"
    assert result.subtotal_amount == Decimal("380.82")
    assert "güçlü bir kanıt değil" in result.message


def test_implied_rate_that_contradicts_the_printed_rate_is_flagged() -> None:
    result = reconcile(None, "30.00", "106.48", tax_rates=[10])  # 10 % would be 9.68
    assert result.status is FLAGGED and result.method is None and result.subtotal_amount is None


def test_implied_without_rates_uses_the_legal_rates() -> None:
    assert reconcile(None, "9.68", "106.48").status is VERIFIED  # fits 10 %
    assert reconcile(None, "40.37", "24.95").status is FLAGGED  # tax larger than total
    assert reconcile(None, "60.00", "100.00").status is FLAGGED  # 150 % is not a VAT rate


def test_stored_implied_subtotal_is_not_evidence() -> None:
    """After a PATCH the stored ``total - tax`` must not make the identity trivially true."""
    wrong = reconcile("94.00", "30.00", "124.00", subtotal_method="implied_rate", tax_rates=[10])
    assert wrong.status is FLAGGED  # 30 on 124 is not 10 %, although 94 + 30 == 124
