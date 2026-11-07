"""Regression tests built from real receipts that once went wrong.

Each case is the OCR output of a real photo (shop / terminal / card details removed).
Rule of this file: when a real receipt is read wrongly, add its OCR rows here *first*,
watch the test fail, then fix the code.
"""

import datetime as dt
from decimal import Decimal

from app.schemas.receipt import Currency, ReconciliationStatus
from app.services.parser import extract_paid_amount, parse_receipt
from app.services.reconciler import reconcile
from tests.helpers import make_ocr

D = Decimal
TODAY = dt.date(2026, 11, 5)

# "TOPLAM *94.00" was read as "TOPLAH 494.00": the "*" became a "4". Also "TOPKDV" -> "TOPKOV".
E_ARSIV_ROWS = [
    ["ORNEK MARKET TSTARBOL"],
    ["E-ARSIY FATURA BILGI FISI"],
    ["11.05.2024 19:01", "NO : 280"],
    ["8 ad X 11.75"],
    ["KEO1 HAHASI 85 G", "220", "94.00"],
    ["TOPKOV", "15.67"],
    ["TOPLAH", "494.00"],
    ["Nakit", "*204.00"],
    ["PARA USTU", "*-110.00"],
]

# Rates at the end of every item line, only TOPKDV + TOPLAM printed; no matrah.
MARKET_ROWS = [
    ["$EHIR MH 123.SK"],
    ["TARIH  01/02/2024"],
    ["OZMO FUN FIGURLU 23G", "%01  *13,50"],
    ["TADPI PILIC BAGET KG", "%01  *102,27"],
    ["FINISH QUANTUM OZEL", "%20  *259,00"],
    ["25TLFINISH110TL", "*149,00."],
    ["ARKO DEGERLI YAGLAR", "%20  *104,50"],
    ["TOPKDV", "*46,60"],
    ["TOPLAM", "*427,42"],
]

# A badly read photo: must never be reported as verified.
GARBLED_ROWS = [
    ["%20  %01  KDV  TOPLAM  TOPKDV  SAAT  TARIH"],
    ["KDV TUTARI", "-", "10.11.2023"],
    ["MF YAC 12345678", "x4,16", "40,37"],
    ["KDV'LI TOPLAM", "F", "*24,95"],
]


def _read(rows: list[list[str]]):
    parsed = parse_receipt(make_ocr(rows), today=TODAY)
    detail = reconcile(
        parsed.subtotal_amount,
        parsed.tax_amount,
        parsed.total_amount,
        subtotal_method="derived_from_rate" if parsed.subtotal_derived else "printed" if parsed.subtotal_amount else None,
        tax_rates=parsed.tax_rates,
        paid_amount=parsed.paid_amount,
    )
    return parsed, detail


def test_star_misread_as_a_digit_is_corrected_by_the_payment_and_the_tax_rate() -> None:
    parsed, detail = _read(E_ARSIV_ROWS)
    assert parsed.total_amount == D("94.00")  # not 494.00
    assert parsed.tax_amount == D("15.67")  # "TOPKOV" is a misread TOPKDV
    assert parsed.paid_amount == D("94.00")  # 204 cash - 110 change
    assert parsed.date == dt.date(2024, 5, 11) and parsed.currency is Currency.TRY
    assert any("494" in warning and "94" in warning for warning in parsed.warnings)  # the user is told
    assert detail.status is ReconciliationStatus.VERIFIED
    assert detail.payment_matches is True


def test_cash_minus_change_is_extracted_as_the_paid_amount() -> None:
    assert extract_paid_amount(["TOPLAM 106,48", "NAKIT 200,00", "PARA USTU 93,52"]) == D("106.48")
    assert extract_paid_amount(["Cash $20.00", "Change $9.20"]) == D("10.80")
    assert extract_paid_amount(["KREDI KARTI 106,48"]) == D("106.48")


def test_ambiguous_payments_are_not_used_as_evidence() -> None:
    assert extract_paid_amount(["NAKIT 110,00"]) is None  # cash only: might include a tip or be more than the bill
    assert extract_paid_amount(["NAKIT 50,00", "KREDI KARTI 56,48", "PARA USTU 0,00"]) is None  # split payment


def test_a_correct_total_is_left_alone() -> None:
    rows = [["ORNEK MARKET"], ["ARA TOPLAM", "96,80"], ["KDV %10", "9,68"], ["TOPLAM", "106,48"], ["NAKIT", "110,00"], ["PARA USTU", "3,52"]]
    parsed, detail = _read(rows)
    assert parsed.total_amount == D("106.48")
    assert not any("corrected" in warning.lower() for warning in parsed.warnings)
    assert detail.status is ReconciliationStatus.VERIFIED and detail.method == "printed"


def test_market_receipt_without_matrah_is_verified_by_the_implied_rate() -> None:
    parsed, detail = _read(MARKET_ROWS)
    assert (parsed.tax_amount, parsed.total_amount) == (D("46.60"), D("427.42"))
    assert parsed.currency is Currency.TRY and parsed.date == dt.date(2024, 2, 1)
    assert detail.status is ReconciliationStatus.VERIFIED and detail.method == "implied_rate"


def test_garbled_receipt_is_never_verified() -> None:
    _, detail = _read(GARBLED_ROWS)
    assert detail.status is ReconciliationStatus.FLAGGED_FOR_REVIEW


def test_payment_that_contradicts_the_total_flags_the_receipt() -> None:
    detail = reconcile("96.80", "9.68", "106.48", paid_amount="94.00")
    assert detail.status is ReconciliationStatus.FLAGGED_FOR_REVIEW
    assert detail.payment_matches is False and "94,00" in detail.message


def test_payment_that_agrees_is_reported() -> None:
    detail = reconcile("96.80", "9.68", "106.48", paid_amount="106.48")
    assert detail.status is ReconciliationStatus.VERIFIED and detail.payment_matches is True
