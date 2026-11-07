"""HTML components: escaping, structure and the rules Markdown needs (one line, no <p>)."""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ui  # noqa: E402

EVIL = '<img src=x onerror="alert(1)"> $5 & "quotes"'


def _assert_markdown_safe(html: str) -> None:
    """Markdown ends an HTML block at a blank line and treats a 4-space indent as code."""
    assert "\n" not in html
    assert not re.search(r"<p[\s>]", html)  # Streamlit styles every <p> in markdown; components use <div>
    assert not re.match(r"\s", html)  # block-level root, nothing before it


def _assert_no_raw_injection(html: str) -> None:
    assert "<img" not in html
    assert 'onerror="' not in html
    assert "&lt;img" in html


def test_esc_escapes_html_and_dollar() -> None:
    assert ui.esc(EVIL) == "&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &#36;5 &amp; &quot;quotes&quot;"
    assert ui.esc(3) == "3"


def test_every_component_is_markdown_safe_and_escapes_user_text() -> None:
    parts = [
        ui.card_title_html(EVIL, EVIL),
        ui.section_title_html(EVIL, EVIL),
        ui.hint_html(EVIL),
        ui.empty_state_html(EVIL, EVIL),
        ui.sample_text_html(EVIL, EVIL),
        ui.status_card_html(
            verified=False, who=EVIL, subtotal=EVIL, tax=EVIL, total=EVIL, explanation=EVIL, difference=EVIL, chips=[EVIL]
        ),
        ui.notes_html([EVIL, EVIL]),
        ui.legend_html(3, EVIL, EVIL),
        ui.kpi_grid_html([ui.Kpi(EVIL, EVIL, unit=EVIL, hint=EVIL, ok_share=0.5)]),
        ui.bars_card_html(EVIL, EVIL, [ui.BarRow(EVIL, 5.0, EVIL, EVIL)], EVIL),
        ui.columns_card_html(EVIL, EVIL, [ui.ColumnRow(EVIL, EVIL, 5.0, EVIL, EVIL)], EVIL),
        ui.table_html(
            [ui.TableRow(EVIL, EVIL, EVIL, EVIL, EVIL, EVIL, True), ui.TableRow("#2", "-", "x", "y", "-", "1", False)]
        ),
    ]
    for html in parts:
        _assert_markdown_safe(html)
        _assert_no_raw_injection(html)


def test_brand_header_is_markdown_safe() -> None:
    html = ui.brand_header_html()
    _assert_markdown_safe(html)
    assert "<h1" in html and "Fiş Mutabakat" in html


def test_status_card_verified_and_flagged() -> None:
    ok = ui.status_card_html(
        verified=True, who="MARKET", subtotal="96,80 ₺", tax="9,68 ₺", total="106,48 ₺", explanation="Tamam."
    )
    assert "fm-status--ok" in ok and ">Mutabık<" in ok and "Fark" not in ok
    assert ok.index("Matrah") < ok.index("KDV") < ok.index("Toplam")  # the ledger reads top to bottom
    bad = ui.status_card_html(
        verified=False,
        who="X",
        subtotal="1",
        tax="2",
        total="9",
        explanation="Uyuşmuyor.",
        difference="+6,00 ₺",
        chips=["OCR güveni 0,97"],
    )
    assert "fm-status--warn" in bad and ">İncelenecek<" in bad and "+6,00 ₺" in bad and "OCR güveni 0,97" in bad
    # status is never colour alone: always an icon and a word
    assert "<svg" in ok and "<svg" in bad


def test_notes_are_empty_without_notes() -> None:
    assert ui.notes_html([]) == ""
    assert "<li>Bir şey</li>" in ui.notes_html(["Bir şey"])


def test_kpi_meter_and_units() -> None:
    html = ui.kpi_grid_html(
        [
            ui.Kpi("Fiş", "8", hint="4 mutabık, 4 incelenecek", icon_name="receipt", ok_share=0.5),
            ui.Kpi("Toplam (TRY)", "1.182,21", unit="₺"),
        ]
    )
    assert 'class="fm-meter"' in html and "width:50.0%" in html
    assert 'class="fm-kpi-unit">₺<' in html
    assert ui.kpi_grid_html([ui.Kpi("A", "1", ok_share=7.0)]).count("width:100.0%") == 1  # shares are clamped


def test_bar_widths_follow_values_and_never_vanish() -> None:
    html = ui.bars_card_html(
        "T", "", [ui.BarRow("big", 100.0, "100"), ui.BarRow("small", 0.5, "0,5"), ui.BarRow("zero", 0.0, "0")], "yok"
    )
    assert "width:100.0%" in html and "width:2.0%" in html and "width:0.0%" in html
    assert "<figure" in html and "<ul" in html


def test_empty_charts_show_the_empty_message() -> None:
    assert "Veri yok" in ui.bars_card_html("T", "", [], "Veri yok")
    assert "Veri yok" in ui.columns_card_html("T", "", [], "Veri yok")


def test_column_heights_are_pixels_and_values_stay_readable() -> None:
    html = ui.columns_card_html(
        "Aylık",
        "₺",
        [ui.ColumnRow("Oca", "2026", 200.0, "200", "200,00 ₺"), ui.ColumnRow("Şub", "2026", 100.0, "100", "100,00 ₺")],
        "yok",
    )
    assert f"height:{ui.COLUMN_MAX_PX}px" in html and f"height:{ui.COLUMN_MAX_PX // 2}px" in html
    assert "200,00 ₺" in html  # exact value for tooltip / screen readers


def test_table_keeps_semantics_and_marks_status_with_text() -> None:
    html = ui.table_html([ui.TableRow("#1", "04.11.2026", "MARKET", "Market", "basılı", "106,48 ₺", True)])
    assert 'role="table"' in html and 'role="columnheader"' in html and html.count('role="cell"') == 7
    assert "Mutabık" in html and "İncelenecek" not in html
    assert 'class="fm-num fm-total">106,48 ₺<' in html  # money is a number cell: right aligned, never truncated


def test_icons_are_decorative() -> None:
    for name in ("check", "alert", "info", "receipt"):
        svg = ui.icon(name)
        assert 'aria-hidden="true"' in svg and 'focusable="false"' in svg
    with pytest.raises(KeyError):
        ui.icon("nope")
