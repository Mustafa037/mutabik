"""Pure helpers of the Streamlit app (no server, no browser)."""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as frontend  # noqa: E402


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1250", "1250.00"), ("1250,50", "1250.50"), ("1.250,50", "1250.50"), ("1,250.50", "1250.50"), ("1.250", "1250.00")],
)
def test_parse_amount_input(text: str, expected: str) -> None:
    assert frontend.parse_amount_input(text) == Decimal(expected)


def test_parse_amount_input_empty_and_invalid() -> None:
    assert frontend.parse_amount_input("  ") is None
    with pytest.raises(ValueError):
        frontend.parse_amount_input("abc")


def test_format_money_turkish_style() -> None:
    assert frontend.format_money(Decimal("1250.5"), "TRY") == "1.250,50 ₺"
    assert frontend.format_money(None, "USD") == "-"


def test_build_changes_only_reports_differences() -> None:
    receipt = {
        "merchant_name": "A", "date": "2026-11-04", "subtotal_amount": 10.0, "tax_amount": 1.0,
        "total_amount": 11.0, "currency": "TRY", "category": None,
    }
    same = {
        "merchant_name": "A", "date": frontend.dt.date(2026, 11, 4), "subtotal_amount": Decimal("10.00"),
        "tax_amount": Decimal("1.00"), "total_amount": Decimal("11.00"), "currency": "TRY", "category": "other",
    }
    assert frontend.build_changes(receipt, same) == {}  # a missing category equals "other"
    changed = {**same, "total_amount": Decimal("12.00"), "category": "grocery"}
    assert frontend.build_changes(receipt, changed) == {"total_amount": "12.00", "category": "grocery"}


def test_bar_rows_filters_currency_labels_and_shares() -> None:
    rows = [
        {"currency": "TRY", "category": "grocery", "total_amount": 75.0},
        {"currency": "TRY", "category": "other", "total_amount": 25.0},
        {"currency": "USD", "category": "grocery", "total_amount": 9.0},
    ]
    result = frontend.bar_rows(rows, "TRY", "category", frontend.category_label, Decimal("100"))
    assert [row.label for row in result] == ["Market", "Diğer"]
    assert [row.text for row in result] == ["75,00 ₺", "25,00 ₺"]
    assert [row.share for row in result] == ["%75", "%25"]
    assert result[0].value == 75.0


def test_bar_rows_without_a_total_have_no_share() -> None:
    rows = [{"currency": "TRY", "merchant_name": "A", "total_amount": 5}]
    assert frontend.bar_rows(rows, "TRY", "merchant_name")[0].share == ""


def test_column_rows_are_chronological_and_limited() -> None:
    rows = [{"currency": "TRY", "month": f"2026-{month:02d}", "total_amount": month * 100} for month in (3, 1, 2)]
    rows.append({"currency": "USD", "month": "2026-01", "total_amount": 1})
    result = frontend.column_rows(rows, "TRY")
    assert [(row.label, row.sublabel) for row in result] == [("Oca", "2026"), ("Şub", "2026"), ("Mar", "2026")]
    assert [row.short for row in result] == ["100", "200", "300"]
    assert result[2].full == "300,00 ₺"
    assert [row.label for row in frontend.column_rows(rows, "TRY", limit=2)] == ["Şub", "Mar"]


def test_takeaway_names_the_biggest_item() -> None:
    rows = frontend.bar_rows(
        [{"currency": "TRY", "category": "grocery", "total_amount": 60}], "TRY", "category", frontend.category_label, Decimal("100")
    )
    assert frontend.takeaway(rows, "₺") == "₺ · en büyük: Market (%60)"
    assert frontend.takeaway([], "₺") == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [(Decimal("1312.21"), "1.312"), (Decimal("427.42"), "427"), (Decimal("5.5"), "5,5"), (Decimal("12"), "12"), (Decimal("0"), "0")],
)
def test_format_compact(value: Decimal, expected: str) -> None:
    assert frontend.format_compact(value) == expected


def test_format_dates_and_months() -> None:
    assert frontend.format_date_tr("2026-11-04") == "04.11.2026"
    assert frontend.format_date_tr(None) == "-"
    assert frontend.format_date_tr("garbage") == "garbage"
    assert frontend.format_month("2026-11") == ("Kas", "2026")
    assert frontend.format_month("nonsense") == ("nonsense", "")
    assert frontend.format_month("2026-13") == ("2026-13", "")


def test_format_int_and_number() -> None:
    assert frontend.format_int(1284) == "1.284"
    assert frontend.format_int(7) == "7"
    assert frontend.format_number(Decimal("1250.5")) == "1.250,50"


def test_percent_text() -> None:
    assert frontend.percent_text(Decimal("55"), Decimal("100")) == "%55"
    assert frontend.percent_text(Decimal("1"), Decimal("0")) == ""
    assert frontend.percent_text(Decimal("1"), None) == ""


def test_ledger_difference_only_for_receipts_that_do_not_add_up() -> None:
    base = {"currency": "TRY", "subtotal_amount": 100.0, "tax_amount": 20.0, "total_amount": 130.0}
    assert frontend.ledger_difference({**base, "reconciliation_status": "flagged_for_review"}) == "+10,00 ₺"
    assert frontend.ledger_difference({**base, "total_amount": 110.0, "reconciliation_status": "flagged_for_review"}) == "−10,00 ₺"
    assert frontend.ledger_difference({**base, "reconciliation_status": "verified"}) is None
    assert frontend.ledger_difference({**base, "subtotal_amount": None, "reconciliation_status": "flagged_for_review"}) is None


def test_receipt_label_and_table_row() -> None:
    item = {
        "id": 7, "merchant_name": None, "date": "2026-11-02", "total_amount": 94.0, "currency": "TRY",
        "category": None, "reconciliation_status": "flagged_for_review", "reconciliation_method": "implied_rate",
    }
    assert frontend.receipt_label(item) == "#7 · İşletme yok · 94,00 ₺ · 02.11.2026"
    row = frontend.table_row(item)
    assert (row.ident, row.date, row.merchant, row.category, row.method, row.total, row.verified) == (
        "#7", "02.11.2026", "İşletme okunamadı", "Diğer", "oran uyumlu", "94,00 ₺", False,
    )
