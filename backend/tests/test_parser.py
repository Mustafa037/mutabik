"""Parser: money strings, dates, locale/currency, amount rows and merchant."""

import datetime as dt
from decimal import Decimal

import pytest

from app.schemas.receipt import Currency
from app.services.parser import (
    Locale,
    DERIVED_SUBTOTAL_WARNING,
    detect_currency,
    detect_locale,
    extract_amounts,
    extract_date,
    extract_tax_rates,
    parse_amount,
    parse_receipt,
)
from tests.helpers import make_ocr

TODAY = dt.date(2026, 11, 5)
D = Decimal


# ---------------------------------------------------------------------------
# parse_amount
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("106,48", "106.48"),
        ("*106,48 TL", "106.48"),
        ("₺106,48", "106.48"),
        ("106,48₺", "106.48"),
        ("1.250,50", "1250.50"),
        ("1,250.50", "1250.50"),
        ("$1,250.50", "1250.50"),
        ("1.250", "1250.00"),  # three digits after one separator = thousands
        ("0,500", "0.50"),
        ("12,5", "12.50"),
        ("1O6,48", "106.48"),  # letter O inside a number
        ("-12,50", "-12.50"),
        ("12,50-", "-12.50"),
        ("(12.50)", "-12.50"),
        ("110 TL", "110.00"),
    ],
)
def test_parse_amount_valid(text: str, expected: str) -> None:
    assert parse_amount(text) == D(expected)


@pytest.mark.parametrize("text", ["", "abc", "1.2.3,4", "12,345,67", "99999999999999"])
def test_parse_amount_invalid(text: str) -> None:
    assert parse_amount(text) is None


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ("TARIH: 04.11.2026  14 : 32", dt.date(2026, 11, 4)),
        ("Date: 2026-11-04", dt.date(2026, 11, 4)),
        ("04/11/2026", dt.date(2026, 11, 4)),
        ("04.11.26", dt.date(2026, 11, 4)),
        ("TARIH l2.O5.2O26", dt.date(2026, 5, 12)),  # OCR digit confusions
        ("04 . 11 . 2026", dt.date(2026, 11, 4)),
    ],
)
def test_extract_date(row: str, expected: dt.date) -> None:
    value, _ = extract_date([row], Currency.TRY, TODAY)
    assert value == expected


def test_extract_date_rejects_impossible_and_future() -> None:
    assert extract_date(["31.02.2026"], Currency.TRY, TODAY)[0] is None
    assert extract_date(["01.01.2031"], Currency.TRY, TODAY)[0] is None
    assert extract_date(["01.01.1999"], Currency.TRY, TODAY)[0] is None


def test_extract_date_keyword_row_wins() -> None:
    value, _ = extract_date(["01.01.2026 KASA 3", "TARIH: 04.11.2026"], Currency.TRY, TODAY)
    assert value == dt.date(2026, 11, 4)


def test_extract_date_slash_is_month_first_for_usd_with_warning() -> None:
    value, warnings = extract_date(["03/04/2026"], Currency.USD, TODAY)
    assert value == dt.date(2026, 3, 4)
    assert warnings


# ---------------------------------------------------------------------------
# locale / currency
# ---------------------------------------------------------------------------
def test_locale_and_currency_turkish() -> None:
    rows = ["ARA TOPLAM 96,80", "KDV %10 9,68", "TOPLAM *106,48 TL"]
    assert detect_locale(rows) is Locale.TR
    assert detect_currency(rows, Locale.TR) is Currency.TRY


def test_locale_and_currency_english() -> None:
    rows = ["Subtotal $10.00", "Tax 8% $0.80", "TOTAL $10.80"]
    assert detect_locale(rows) is Locale.EN
    assert detect_currency(rows, Locale.EN) is Currency.USD


def test_turkish_lira_symbol_is_recognised() -> None:
    rows = ["TOPLAM ₺106,48"]
    assert detect_currency(rows, detect_locale(rows)) is Currency.TRY


# ---------------------------------------------------------------------------
# amount rows
# ---------------------------------------------------------------------------
def test_amounts_basic_turkish() -> None:
    amounts, _ = extract_amounts(
        ["SUT 1L x2  45,90", "ARA TOPLAM  96,80", "KDV %10  9,68", "TOPLAM  *106,48 TL", "NAKIT  110,00"]
    )
    assert amounts == {"subtotal": D("96.80"), "tax": D("9.68"), "total": D("106.48")}


def test_amounts_ignore_cash_and_change_rows() -> None:
    amounts, _ = extract_amounts(["TOPLAM 106,48", "NAKIT 200,00", "PARA USTU 93,52"])
    assert amounts["total"] == D("106.48")


@pytest.mark.parametrize("keyword", ["T0PLAM", "TOPLAN", "TOPLAM:", "toplam"])
def test_amounts_tolerate_ocr_errors_in_keywords(keyword: str) -> None:
    amounts, _ = extract_amounts([f"{keyword}  106,48"])
    assert amounts["total"] == D("106.48")


def test_amounts_sum_multiple_tax_rates() -> None:
    amounts, _ = extract_amounts(["KDV %1  0,50", "KDV %10  9,68", "TOPLAM 106,98"])
    assert amounts["tax"] == D("10.18")


def test_amounts_strong_total_beats_weak() -> None:
    amounts, _ = extract_amounts(["TOPLAM 100,00", "GENEL TOPLAM 120,00"])
    assert amounts["total"] == D("120.00")


def test_amounts_value_on_the_next_row() -> None:
    amounts, _ = extract_amounts(["TOPLAM", "106,48"])
    assert amounts["total"] == D("106.48")


def test_amounts_split_decimal_from_ocr() -> None:
    amounts, _ = extract_amounts(["TOPLAM 106, 48"])
    assert amounts["total"] == D("106.48")


def test_amounts_english() -> None:
    amounts, _ = extract_amounts(["Subtotal  $10.00", "Tax 8%  $0.80", "TOTAL  $10.80"])
    assert amounts == {"subtotal": D("10.00"), "tax": D("0.80"), "total": D("10.80")}


# ---------------------------------------------------------------------------
# whole receipt
# ---------------------------------------------------------------------------
def test_parse_receipt_turkish_end_to_end() -> None:
    ocr = make_ocr(
        [
            ["ORNEK MARKET A.S"],
            ["Cankaya Cad. No:l2 Ankara"],
            ["TARIH: 04.11.2026", "14 : 32"],
            ["SUT lL x2", "45,90"],
            ["ARA TOPLAM", "96,80"],
            ["KDV %10", "9,68"],
            ["TOPLAM", "*106,48 TL"],
            ["TESEKKUR EDERIZ"],
        ]
    )
    parsed = parse_receipt(ocr, today=TODAY)
    assert parsed.merchant_name == "ORNEK MARKET A.S"
    assert parsed.date == dt.date(2026, 11, 4)
    assert (parsed.subtotal_amount, parsed.tax_amount, parsed.total_amount) == (D("96.80"), D("9.68"), D("106.48"))
    assert parsed.currency is Currency.TRY
    assert parsed.locale is Locale.TR


def test_parse_receipt_empty_ocr() -> None:
    parsed = parse_receipt(make_ocr([]), today=TODAY)
    assert parsed.total_amount is None
    assert parsed.warnings


# ---------------------------------------------------------------------------
# Findings from a real Turkish till receipt
# ---------------------------------------------------------------------------
REAL_RECEIPT_ROWS = [
    ["$AHINTEPE MH 637.SK"],
    ["TARIH  : 01/02/2024"],
    ["OZMO FU FIGURLU 23G", "%01  *13,50"],
    ["TADPI PILIC BAGET KG", "%01  *102,27"],
    ["FINISH QUANTUM OZEL", "%20  *259,00"],
    ["25TLFINISH110TL", "*149,00."],
    ["ARKO DEGERLI YAGLAR", "$20  *104,50"],
    ["TOPKDV", "*46,60"],
    ["TOPLAM", "*427,42"],
]


def test_dollar_sign_is_not_a_currency_on_a_turkish_receipt() -> None:
    parsed = parse_receipt(make_ocr(REAL_RECEIPT_ROWS), today=TODAY)
    assert parsed.currency is Currency.TRY
    assert parsed.date == dt.date(2024, 2, 1)  # day first: not 2 January


def test_explicit_usd_still_wins_on_a_turkish_receipt() -> None:
    rows = ["TOPLAM  USD 10,00", "KDV %10  0,91"]
    assert detect_currency(rows, Locale.TR) is Currency.USD


def test_tax_rates_are_read_from_item_lines() -> None:
    parsed = parse_receipt(make_ocr(REAL_RECEIPT_ROWS), today=TODAY)
    assert parsed.tax_rates == (D(1), D(20))  # "$20" is an OCR-misread "%20"
    assert parsed.subtotal_amount is None and parsed.tax_amount == D("46.60") and parsed.total_amount == D("427.42")


@pytest.mark.parametrize(
    ("rows", "dollar", "expected"),
    [
        (["KDV %10  9,68"], False, (D(10),)),
        (["EKMEK %01 *12,50", "SUT %08 *30,00"], False, (D(1), D(8))),
        (["Tax 8%  $0.80"], False, (D(8),)),
        (["AYRAN $20 *5,00"], True, (D(20),)),
        (["PRICE $20 *5,00"], False, ()),  # a dollar sign is only a percent sign on Turkish receipts
        (["%50 INDIRIM 5,00"], False, ()),  # not a VAT rate, and a discount row
        (["%10 INDIRIM 5,00"], False, ()),  # discount rows are skipped even for real rates
    ],
)
def test_extract_tax_rates(rows: list[str], dollar: bool, expected: tuple[Decimal, ...]) -> None:
    assert extract_tax_rates(rows, dollar_is_percent=dollar) == expected


def test_matrah_is_derived_even_when_topkdv_is_printed() -> None:
    amounts, warnings = extract_amounts(["KDV %10  9,68", "TOPKDV  9,68", "TOPLAM  106,48"])
    assert amounts["subtotal"] == D("96.80")
    assert DERIVED_SUBTOTAL_WARNING in warnings


def test_matrah_is_not_derived_when_topkdv_disagrees_with_the_rate_rows() -> None:
    amounts, _ = extract_amounts(["KDV %10  9,68", "TOPKDV  19,68", "TOPLAM  106,48"])
    assert amounts["subtotal"] is None
