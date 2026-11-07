"""Receipt-level evaluation metrics and the draft-label helper (no OCR model needed)."""

from decimal import Decimal

from app.schemas.receipt import Currency, ReconciliationStatus
from eval.draft import draft_entry
from eval.metrics import Record, condition_of, is_verified, render_receipt_metrics, summarize


def test_condition_is_read_from_the_file_name() -> None:
    assert condition_of("migros__kotu.jpg") == "kotu"
    assert condition_of("migros.jpg") == "genel"
    assert condition_of("a__b__c.png") == "b__c"


def test_is_verified_accepts_strings_and_enums() -> None:
    assert is_verified("verified") and is_verified(ReconciliationStatus.VERIFIED)
    assert not is_verified("flagged_for_review") and not is_verified(None)


def test_summary_separates_caught_missed_and_false_alarms() -> None:
    records = [
        Record("a.jpg", "iyi", (), True),  # right and verified: automatic
        Record("b.jpg", "iyi", (), False),  # right but flagged: false alarm
        Record("c.jpg", "kotu", ("total_amount",), False),  # wrong and flagged: caught
        Record("d.jpg", "kotu", ("date",), True),  # wrong but verified: the dangerous one
    ]
    summary = summarize(records)
    assert summary["auto_correct"] == 1 and summary["auto_correct_rate"] == 0.25
    assert summary["caught"] == 1 and summary["catch_rate"] == 0.5
    assert summary["missed"] == [("d.jpg", ("date",))]
    assert summary["false_alarms"] == 1 and summary["false_alarm_rate"] == 0.5


def test_a_receipt_that_really_does_not_add_up_is_not_a_false_alarm() -> None:
    summary = summarize([Record("k.jpg", "iyi", (), False, expect_flag=True), Record("a.jpg", "iyi", (), True)])
    assert summary["false_alarms"] == 0 and summary["auto_correct"] == 1


def test_rates_are_none_without_a_denominator() -> None:
    summary = summarize([Record("a.jpg", "iyi", (), True)])
    assert summary["catch_rate"] is None and summary["auto_correct_rate"] == 1.0
    assert summarize([])["auto_correct_rate"] is None


def test_report_lists_missed_errors_and_conditions() -> None:
    text = render_receipt_metrics(
        [Record("a.jpg", "iyi", (), True), Record("d.jpg", "kotu", ("date",), True)]
    )
    assert "Kaçan hata" in text and "`d.jpg`" in text and "| kotu |" in text and "| iyi |" in text
    assert render_receipt_metrics([]) == ""


def test_draft_entry_converts_values_and_notes_what_is_missing() -> None:
    got = {
        "merchant_name": "MARKET",
        "date": "2026-11-04",
        "total_amount": Decimal("94.00"),
        "tax_amount": None,
        "currency": Currency.TRY,
        "category": None,
        "status": ReconciliationStatus.FLAGGED_FOR_REVIEW,
    }
    entry = draft_entry(got, "x__egik.jpg")
    assert entry["total_amount"] == "94.00" and entry["currency"] == "TRY" and entry["tax_amount"] is None
    notes = " ".join(entry["_oku"])
    assert "koşul: egik" in notes and "flagged_for_review" in notes and "tax_amount" in notes
    assert "okunamadı" in " ".join(draft_entry({}, "y.jpg")["_oku"])
