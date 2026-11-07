"""Reconciliation engine: checks ``subtotal + tax == total`` within a tolerance.

Result states (``ReconciliationStatus``):

* ``VERIFIED`` - all three amounts are present, signs agree and
  ``|total - (subtotal + tax)| <= tolerance``.
* ``FLAGGED_FOR_REVIEW`` - anything else: a mismatch, a missing amount or
  inconsistent signs. The reason is written to ``ReconciliationDetail.message``.

A receipt is never verified from *unrelated* data. When the subtotal (matrah) is
not printed there are two weaker but still independent checks:

* the subtotal was rebuilt from ``KDV %rate`` rows (``derived_from_rate``): the identity
  is checked as usual, because the subtotal does not depend on the total;
* nothing to rebuild it from (``implied_rate``): the VAT rate implied by ``tax`` and
  ``total`` must agree with the rates seen on the receipt (or lie within the legal range).
  ``total - tax`` is *never* just accepted as the subtotal.

The ``method`` of the result tells how much evidence stands behind ``VERIFIED``.

A second, independent check uses the payment: ``cash - change`` (or the card amount) must
equal the total. A receipt whose payment contradicts the total is flagged even when the
tax arithmetic works, because one of the two amounts was probably misread.
"""

import logging
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final, Optional, Union

from app.schemas.receipt import ReconciliationDetail, ReconciliationStatus
from app.services.formatting import format_tr

__all__ = [
    "DEFAULT_TOLERANCE",
    "METHOD_PRINTED",
    "METHOD_DERIVED",
    "METHOD_IMPLIED",
    "METHOD_MANUAL",
    "reconcile",
]

logger = logging.getLogger(__name__)

#: Absolute tolerance in currency units. Receipts round tax per rate / per line,
#: so a difference of one or two kuruş / cents is normal.
DEFAULT_TOLERANCE: Final[Decimal] = Decimal("0.02")
_CENT: Final[Decimal] = Decimal("0.01")

#: How the subtotal was obtained (stored in ``expenses.reconciliation_method``).
METHOD_PRINTED: Final[str] = "printed"
METHOD_DERIVED: Final[str] = "derived_from_rate"
METHOD_IMPLIED: Final[str] = "implied_rate"
METHOD_MANUAL: Final[str] = "manual"  # corrected by the user

#: Turkish VAT rates (current 1 / 10 / 20, older 8 / 18); used when no rate is printed.
LEGAL_RATES: Final[tuple[Decimal, ...]] = tuple(Decimal(rate) for rate in (1, 8, 10, 18, 20))
#: Largest accepted gap between the printed tax and ``total * r / (100 + r)``. Per-line
#: rounding of many items adds up, so this is looser than the 0.02 identity tolerance.
IMPLIED_TOLERANCE: Final[Decimal] = Decimal("0.10")
#: Slack (percentage points) around the lowest / highest rate for mixed-rate receipts.
RATE_SLACK: Final[Decimal] = Decimal("0.5")

Number = Union[Decimal, int, float, str]


def _to_decimal(value: Optional[Number], name: str) -> Optional[Decimal]:
    """Coerce a number to a two-decimal ``Decimal`` (``None`` stays ``None``).

    Raises:
        ValueError: If ``value`` is not a finite number.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, not a boolean.")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        if not number.is_finite():
            raise ValueError(f"{name} must be finite.")
        return number.quantize(_CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(f"{name} is not a valid amount: {value!r}.") from exc


def _pct(rate: Decimal) -> str:
    """``Decimal('20.00')`` -> ``20`` (rates are shown without trailing zeros)."""
    return f"{rate.normalize():f}"


def _check_implied(
    tax: Decimal, total: Decimal, rates: Sequence[Decimal]
) -> tuple[Optional[Decimal], str]:
    """Plausibility of ``tax`` against ``total`` when the subtotal is unknown.

    Single-rate fit: ``tax ~ total * r / (100 + r)`` for one of the rates. Mixed rates:
    the blended rate ``tax / (total - tax)`` lies between the lowest and highest rate.
    Rates come from the receipt; without any, the legal Turkish rates are used.

    Returns:
        ``(subtotal, message)``; ``subtotal`` is ``total - tax`` when plausible, else ``None``.
    """
    if total <= 0 or tax < 0 or tax >= total:
        return None, (
            f"Doğrulanamadı: KDV ({format_tr(tax)}) toplamın ({format_tr(total)}) makul bir parçası görünmüyor; "
            "rakamlardan biri yanlış okunmuş olabilir."
        )
    subtotal = total - tax
    blended = tax / subtotal * 100
    candidates = sorted(set(rates)) or list(LEGAL_RATES)
    source = "fişteki KDV oranlarıyla" if rates else "yasal KDV oranlarıyla"
    for rate in candidates:
        if abs(tax - total * rate / (100 + rate)) <= IMPLIED_TOLERANCE:
            return subtotal, (
                f"Matrah fişte basılı değil; KDV tutarı %{_pct(rate)} oranıyla uyumlu, "
                "matrah toplamdan hesaplandı."
            )
    low, high = candidates[0], candidates[-1]
    if low != high and low - RATE_SLACK <= blended <= high + RATE_SLACK:
        return subtotal, (
            f"Matrah fişte basılı değil; KDV tutarı {source} (%{_pct(low)}–%{_pct(high)}) uyumlu, "
            "matrah toplamdan hesaplandı. Basılı matrah kadar güçlü bir kanıt değil."
        )
    return None, (
        f"Doğrulanamadı: KDV tutarı %{blended:.1f} orana denk geliyor, bu {source} "
        f"({', '.join(f'%{_pct(rate)}' for rate in candidates)}) uyuşmuyor; bir rakam yanlış okunmuş olabilir."
    )


def reconcile(
    subtotal_amount: Optional[Number],
    tax_amount: Optional[Number],
    total_amount: Optional[Number],
    tolerance: Number = DEFAULT_TOLERANCE,
    *,
    subtotal_method: Optional[str] = None,
    tax_rates: Sequence[Number] = (),
    paid_amount: Optional[Number] = None,
) -> ReconciliationDetail:
    """Check the tax arithmetic, then cross-check the result against the payment.

    See :func:`_reconcile_tax` for the arguments of the tax check. ``paid_amount`` is
    what the customer paid (``cash - change`` or the card amount); when given and
    different from the total (beyond ``tolerance``) the receipt is flagged.
    """
    detail = _reconcile_tax(
        subtotal_amount, tax_amount, total_amount, tolerance, subtotal_method=subtotal_method, tax_rates=tax_rates
    )
    paid = _to_decimal(paid_amount, "paid_amount")
    if paid is None or detail.total_amount is None:
        return detail
    limit = detail.tolerance
    if abs(paid - detail.total_amount) <= limit:
        return detail.model_copy(
            update={"payment_amount": paid, "payment_matches": True, "message": f"{detail.message} Ödenen tutar da uyuşuyor."}
        )
    return detail.model_copy(
        update={
            "status": ReconciliationStatus.FLAGGED_FOR_REVIEW,
            "payment_amount": paid,
            "payment_matches": False,
            "message": (
                f"Ödeme uyuşmuyor: ödenen {format_tr(paid)} (nakit − para üstü / kart) ama toplam "
                f"{format_tr(detail.total_amount)} okundu. Biri yanlış okunmuş olabilir; fişi kontrol edin."
            ),
        }
    )


def _reconcile_tax(
    subtotal_amount: Optional[Number],
    tax_amount: Optional[Number],
    total_amount: Optional[Number],
    tolerance: Number = DEFAULT_TOLERANCE,
    *,
    subtotal_method: Optional[str] = None,
    tax_rates: Sequence[Number] = (),
) -> ReconciliationDetail:
    """Verify ``subtotal + tax == total`` and decide the review flag.

    Args:
        subtotal_amount: Pre-tax amount (matrah).
        tax_amount: Tax amount (KDV / VAT / sales tax).
        total_amount: Grand total.
        tolerance: Largest absolute difference still accepted (``>= 0``).
        subtotal_method: How ``subtotal_amount`` was obtained (``printed``,
            ``derived_from_rate``, ``manual`` or ``implied_rate``). ``implied_rate``
            means the stored subtotal is just ``total - tax``: it is ignored and the
            implied-rate check is run again.
        tax_rates: VAT rates (in %) seen on the receipt, for the implied-rate check.

    Returns:
        A :class:`ReconciliationDetail` with ``expected_total`` (``subtotal +
        tax``), the signed ``difference`` (``total - expected_total``), the
        ``method`` (``None`` when nothing could be verified) and a human-readable
        ``message``. With ``implied_rate`` the detail carries the derived subtotal.

    Raises:
        ValueError: If an amount or the tolerance is not a valid finite number,
            or the tolerance is negative.
    """
    subtotal = _to_decimal(subtotal_amount, "subtotal_amount")
    tax = _to_decimal(tax_amount, "tax_amount")
    total = _to_decimal(total_amount, "total_amount")
    limit = _to_decimal(tolerance, "tolerance")
    if limit is None or limit < 0:
        raise ValueError("tolerance must be a non-negative number.")

    rates = [_to_decimal(rate, "tax_rate") or Decimal(0) for rate in tax_rates]
    if subtotal_method == METHOD_IMPLIED:
        subtotal = None  # a stored "total - tax" is not evidence; re-check from the tax rate

    def build(
        status: ReconciliationStatus,
        message: str,
        expected: Optional[Decimal] = None,
        difference: Optional[Decimal] = None,
        method: Optional[str] = None,
        subtotal_value: Optional[Decimal] = subtotal,
    ) -> ReconciliationDetail:
        return ReconciliationDetail(
            status=status,
            method=method,
            subtotal_amount=subtotal_value,
            tax_amount=tax,
            total_amount=total,
            expected_total=expected,
            difference=difference,
            tolerance=limit,
            message=message,
        )

    if subtotal is None and tax is not None and total is not None:
        implied_subtotal, message = _check_implied(tax, total, rates)
        if implied_subtotal is None:
            return build(ReconciliationStatus.FLAGGED_FOR_REVIEW, message)
        return build(
            ReconciliationStatus.VERIFIED, message, total, Decimal("0.00"), METHOD_IMPLIED, implied_subtotal
        )

    if subtotal is None or tax is None or total is None:
        missing = [
            label
            for label, value in (("matrah", subtotal), ("KDV", tax), ("toplam", total))
            if value is None
        ]
        message = (
            f"Doğrulanamadı: {', '.join(missing)} okunamadı. Fişi net ışıkta, tamamı görünecek şekilde "
            "tekrar çekin ya da değeri elle girin."
        )
        logger.debug("Reconciliation flagged: %s", message)
        return build(ReconciliationStatus.FLAGGED_FOR_REVIEW, message)

    expected_total = subtotal + tax
    difference = total - expected_total

    non_zero = [value for value in (subtotal, tax, total) if value != 0]
    if any(value > 0 for value in non_zero) and any(value < 0 for value in non_zero):
        return build(
            ReconciliationStatus.FLAGGED_FOR_REVIEW,
            "Tutarların işaretleri tutarsız (artı ve eksi karışık); lütfen kontrol edin.",
            expected_total,
            difference,
        )

    if abs(difference) <= limit:
        method = subtotal_method if subtotal_method in (METHOD_DERIVED, METHOD_MANUAL) else METHOD_PRINTED
        message = {
            METHOD_DERIVED: "Matrah fişte basılı değildi; KDV oranından hesaplandı ve toplamla uyuşuyor.",
            METHOD_MANUAL: "Girdiğiniz matrah ve KDV toplamla uyuşuyor.",
        }.get(method, "Matrah ve KDV toplamla uyuşuyor.")
        return build(ReconciliationStatus.VERIFIED, message, expected_total, difference, method)

    message = (
        f"Tutarlar uyuşmuyor: matrah {format_tr(subtotal)} + KDV {format_tr(tax)} = {format_tr(expected_total)} "
        f"ama toplam {format_tr(total)} (fark {'+' if difference > 0 else '−'}{format_tr(abs(difference))}). "
        "Bir rakam yanlış okunmuş olabilir; lütfen kontrol edin."
    )
    logger.debug("Reconciliation flagged: %s", message)
    return build(ReconciliationStatus.FLAGGED_FOR_REVIEW, message, expected_total, difference)
