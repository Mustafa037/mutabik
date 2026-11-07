"""HTML building blocks of the Streamlit UI.

Every function takes ready-to-show strings (already formatted, Turkish) and returns one
line of HTML for ``st.markdown(..., unsafe_allow_html=True)``. Rules:

* **Escaping.** All dynamic text goes through :func:`esc`, so a merchant name such as
  ``<img onerror=...>`` is shown as text. ``$`` is escaped too, because the Markdown
  pipeline treats ``$...$`` as maths.
* **One line, block-level root.** Markdown ends an HTML block at a blank line and treats a
  4-space indent as code, so the output never contains either.
* **Not colour alone.** Status is always icon + text (Apple HIG, "inclusive color").

The matching CSS lives in ``styles.css`` (design tokens, light and dark).
"""

from dataclasses import dataclass
from html import escape
from typing import Optional, Sequence

__all__ = [
    "BarRow",
    "ColumnRow",
    "Kpi",
    "TableRow",
    "bars_card_html",
    "brand_header_html",
    "card_title_html",
    "charts_grid_html",
    "columns_card_html",
    "empty_state_html",
    "esc",
    "hint_html",
    "icon",
    "kpi_grid_html",
    "legend_html",
    "notes_html",
    "sample_text_html",
    "section_title_html",
    "status_card_html",
    "table_html",
]

#: Height in px of the tallest column of a column chart (the card is sized for it).
COLUMN_MAX_PX = 150

#: Stroke icons on a 24x24 grid (drawn with ``currentColor``).
_ICONS: dict[str, str] = {
    "check": '<circle cx="12" cy="12" r="9"/><path d="m8.2 12.4 2.6 2.6 5-5.4"/>',
    "alert": (
        '<path d="M12 4.4 3.3 19.2a1 1 0 0 0 .87 1.5h15.66a1 1 0 0 0 .87-1.5L12 4.4Z"/>'
        '<path d="M12 10v4"/><path d="M12 17.3h.01"/>'
    ),
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 7.7h.01"/>',
    "receipt": (
        '<path d="M6.5 3.5h11v17l-2.75-1.8L12 20.5l-2.75-1.8L6.5 20.5v-17Z"/>'
        '<path d="M9.5 8h5"/><path d="M9.5 11.5h5"/><path d="M9.5 15h2.5"/>'
    ),
}


def esc(text: object) -> str:
    """Escape ``text`` for HTML (and ``$`` for Markdown maths)."""
    return escape(str(text), quote=True).replace("$", "&#36;")


def icon(name: str, extra_class: str = "") -> str:
    """Inline SVG icon (decorative: ``aria-hidden``)."""
    paths = _ICONS[name]
    css = f"fm-ico {extra_class}".strip()
    return f'<svg class="{css}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">{paths}</svg>'


# ---------------------------------------------------------------------------
# Page frame
# ---------------------------------------------------------------------------
def brand_header_html() -> str:
    """Brand mark, name and one-line description at the top of the page."""
    return (
        '<header class="fm-appbar">'
        f'<span class="fm-brandmark">{icon("receipt")}</span>'
        "<div>"
        '<h1 class="fm-brandname">Fiş Mutabakat</h1>'
        '<div class="fm-brandsub">Fişi okuyun, Matrah + KDV = Toplam kuralıyla doğrulayın, harcamaları izleyin.</div>'
        "</div>"
        "</header>"
    )


def card_title_html(title: str, subtitle: str = "") -> str:
    """Title (and optional one-line description) at the top of a card."""
    sub = f'<div class="fm-card-sub">{esc(subtitle)}</div>' if subtitle else ""
    return f'<div class="fm-card-head"><div><h2 class="fm-card-title">{esc(title)}</h2>{sub}</div></div>'


def section_title_html(title: str, subtitle: str = "") -> str:
    """Heading of a page section (not inside a card)."""
    sub = f'<div class="fm-section-sub">{esc(subtitle)}</div>' if subtitle else ""
    return f'<div class="fm-section"><h2 class="fm-section-title">{esc(title)}</h2>{sub}</div>'


def hint_html(text: str) -> str:
    """Quiet inline hint with an info icon."""
    return f'<div class="fm-note">{icon("info")}<div class="fm-note-text">{esc(text)}</div></div>'


def empty_state_html(title: str, text: str, icon_name: str = "receipt") -> str:
    """Empty state: says what happened and what to do next."""
    return (
        '<div class="fm-empty">'
        f'<span class="fm-empty-icon">{icon(icon_name)}</span>'
        f'<div class="fm-empty-title">{esc(title)}</div>'
        f'<div class="fm-empty-text">{esc(text)}</div>'
        "</div>"
    )


def sample_text_html(title: str, hint: str) -> str:
    """Title and one-line hint of a sample receipt row."""
    return f'<div class="fm-sample-title">{esc(title)}</div><div class="fm-sample-hint">{esc(hint)}</div>'


# ---------------------------------------------------------------------------
# Result of one receipt
# ---------------------------------------------------------------------------
def status_card_html(
    *,
    verified: bool,
    who: str,
    subtotal: str,
    tax: str,
    total: str,
    explanation: str,
    difference: Optional[str] = None,
    chips: Sequence[str] = (),
) -> str:
    """Reconciliation result: status, one explanation and the ``subtotal + tax = total`` ledger.

    Args:
        verified: ``True`` for "Mutabık", ``False`` for "İncelenecek".
        who: Merchant, date and category on one line.
        subtotal: Formatted subtotal (matrah).
        tax: Formatted tax (KDV).
        total: Formatted total.
        explanation: One short sentence (the server's message).
        difference: Formatted signed difference, shown when the receipt does not add up.
        chips: Small facts such as OCR confidence and processing time.
    """
    tone = "ok" if verified else "warn"
    name = "Mutabık" if verified else "İncelenecek"
    chip_html = "".join(f'<span class="fm-chip">{esc(chip)}</span>' for chip in chips)
    diff_row = (
        f'<div class="fm-ledger-row fm-ledger-row--diff"><dt>Fark</dt><dd>{esc(difference)}</dd></div>'
        if difference
        else ""
    )
    explain = f'<div class="fm-explain">{esc(explanation)}</div>' if explanation else ""
    return (
        f'<section class="fm-card fm-status fm-status--{tone}" aria-live="polite">'
        '<div class="fm-card-head">'
        '<div class="fm-status-title">'
        f'<span class="fm-status-icon">{icon("check" if verified else "alert")}</span>'
        f'<div><h3 class="fm-status-name">{name}</h3><div class="fm-status-who">{esc(who)}</div></div>'
        "</div>"
        f'<div class="fm-chips">{chip_html}</div>'
        "</div>"
        f"{explain}"
        '<dl class="fm-ledger">'
        f'<div class="fm-ledger-row"><dt>Matrah</dt><dd>{esc(subtotal)}</dd></div>'
        f'<div class="fm-ledger-row"><dt><span class="fm-op">+</span>KDV</dt><dd>{esc(tax)}</dd></div>'
        f'<div class="fm-ledger-row fm-ledger-row--total"><dt><span class="fm-op">=</span>Toplam</dt>'
        f"<dd>{esc(total)}</dd></div>"
        f"{diff_row}"
        "</dl>"
        "</section>"
    )


def notes_html(notes: Sequence[str]) -> str:
    """Things the user should check, as one calm callout (empty string when there are none)."""
    if not notes:
        return ""
    items = "".join(f"<li>{esc(note)}</li>" for note in notes)
    return (
        f'<div class="fm-note" role="note">{icon("info")}'
        f'<div><div class="fm-note-title">Dikkat edilmesi gerekenler</div><ul>{items}</ul></div></div>'
    )


def legend_html(count: int, high: str, medium: str) -> str:
    """Legend for the confidence colours drawn on the receipt image."""
    return (
        '<ul class="fm-legend">'
        f"<li>{esc(count)} metin kutusu</li>"
        f'<li><span class="fm-dot fm-dot--ok"></span>Güven {esc(high)} ve üstü</li>'
        f'<li><span class="fm-dot fm-dot--mid"></span>{esc(medium)} ve üstü</li>'
        '<li><span class="fm-dot fm-dot--low"></span>Daha düşük</li>'
        "</ul>"
    )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Kpi:
    """One key figure.

    Attributes:
        label: What it counts.
        value: Formatted number (no currency symbol).
        unit: Currency symbol or unit shown small next to the value.
        hint: One quiet line below the value.
        tone: ``neutral``, ``ok`` or ``warn`` (colours the small icon).
        icon_name: Icon shown before the label (``""`` for none).
        ok_share: ``0..1`` share of verified receipts; draws the meter under the value.
    """

    label: str
    value: str
    unit: str = ""
    hint: str = ""
    tone: str = "neutral"
    icon_name: str = ""
    ok_share: Optional[float] = None


def kpi_grid_html(items: Sequence[Kpi]) -> str:
    """Responsive grid of key figures."""
    cards: list[str] = []
    for item in items:
        ico = icon(item.icon_name) if item.icon_name else ""
        unit = f'<span class="fm-kpi-unit">{esc(item.unit)}</span>' if item.unit else ""
        hint = f'<div class="fm-kpi-hint">{esc(item.hint)}</div>' if item.hint else ""
        meter = ""
        if item.ok_share is not None:
            ok = max(0.0, min(1.0, item.ok_share)) * 100
            meter = (
                f'<div class="fm-meter" role="img" aria-label="{esc(item.hint)}">'
                f'<span class="fm-meter-ok" style="width:{ok:.1f}%"></span>'
                f'<span class="fm-meter-warn" style="width:{100 - ok:.1f}%"></span></div>'
            )
        cards.append(
            f'<article class="fm-card fm-kpi fm-kpi--{esc(item.tone)}">'
            f'<h3 class="fm-kpi-label">{ico}{esc(item.label)}</h3>'
            f'<div class="fm-kpi-value">{esc(item.value)}{unit}</div>{hint}{meter}</article>'
        )
    return f'<div class="fm-kpis">{"".join(cards)}</div>'


@dataclass(frozen=True)
class BarRow:
    """One bar of a ranked bar chart (``value`` sets the length, ``text`` is shown)."""

    label: str
    value: float
    text: str
    share: str = ""


@dataclass(frozen=True)
class ColumnRow:
    """One column of a column chart; ``short`` is the compact label above it."""

    label: str
    sublabel: str
    value: float
    short: str
    full: str


def _chart_head(title: str, subtitle: str) -> str:
    """Title and takeaway line of a chart card."""
    sub = f'<div class="fm-card-sub">{esc(subtitle)}</div>' if subtitle else ""
    return f'<figcaption><h3 class="fm-card-title">{esc(title)}</h3>{sub}</figcaption>'


def bars_card_html(title: str, subtitle: str, rows: Sequence[BarRow], empty: str) -> str:
    """Ranked horizontal bars with the exact value written next to every bar."""
    if not rows:
        return f'<figure class="fm-card fm-chart">{_chart_head(title, "")}{empty_state_html(empty, "")}</figure>'
    top = max((row.value for row in rows), default=0.0) or 1.0
    items = []
    for row in rows:
        width = max(2.0, row.value / top * 100) if row.value > 0 else 0.0
        share = f'<span class="fm-bar-share">{esc(row.share)}</span>' if row.share else ""
        items.append(
            f'<li><span class="fm-bar-label" title="{esc(row.label)}">{esc(row.label)}</span>'
            f'<span class="fm-bar-value">{esc(row.text)}{share}</span>'
            f'<span class="fm-bar-track" aria-hidden="true"><span class="fm-bar-fill" style="width:{width:.1f}%"></span></span></li>'
        )
    return f'<figure class="fm-card fm-chart">{_chart_head(title, subtitle)}<ul class="fm-bars">{"".join(items)}</ul></figure>'


def columns_card_html(title: str, subtitle: str, rows: Sequence[ColumnRow], empty: str) -> str:
    """Column chart over time; exact values are in the tooltip and the accessible label."""
    if not rows:
        return f'<figure class="fm-card fm-chart">{_chart_head(title, "")}{empty_state_html(empty, "")}</figure>'
    top = max((row.value for row in rows), default=0.0) or 1.0
    items = []
    for row in rows:
        height = max(3, round(row.value / top * COLUMN_MAX_PX)) if row.value > 0 else 0
        sub = f"<small>{esc(row.sublabel)}</small>" if row.sublabel else ""
        items.append(
            f'<li title="{esc(row.label)} {esc(row.sublabel)}: {esc(row.full)}">'
            f'<span class="fm-col-val">{esc(row.short)}</span>'
            f'<span class="fm-col-bar" style="height:{height}px"></span>'
            f'<span class="fm-col-label">{esc(row.label)}{sub}</span>'
            f'<span class="fm-sr">{esc(row.full)}</span></li>'
        )
    return f'<figure class="fm-card fm-chart">{_chart_head(title, subtitle)}<ol class="fm-cols">{"".join(items)}</ol></figure>'


def charts_grid_html(cards: Sequence[str]) -> str:
    """Lay chart cards out in a responsive grid."""
    return f'<div class="fm-charts">{"".join(cards)}</div>'


@dataclass(frozen=True)
class TableRow:
    """One receipt row of the "latest receipts" table (all strings are ready to show)."""

    ident: str
    date: str
    merchant: str
    category: str
    method: str
    total: str
    verified: bool


def table_html(rows: Sequence[TableRow]) -> str:
    """Accessible receipts table: numbers right-aligned, status as icon + text."""
    body = []
    for row in rows:
        pill = (
            f'<span class="fm-pill fm-pill--ok">{icon("check")}Mutabık</span>'
            if row.verified
            else f'<span class="fm-pill fm-pill--warn">{icon("alert")}İncelenecek</span>'
        )
        # explicit roles keep the table semantics when the phone layout changes ``display``
        body.append(
            '<tr role="row">'
            f'<td role="cell" class="fm-id fm-hide-sm">{esc(row.ident)}</td>'
            f'<td role="cell" class="fm-date">{esc(row.date)}</td>'
            f'<td role="cell" class="fm-merchant-cell"><span class="fm-merchant" title="{esc(row.merchant)}">{esc(row.merchant)}</span></td>'
            f'<td role="cell" class="fm-hide-sm"><span class="fm-pill fm-pill--neutral">{esc(row.category)}</span></td>'
            f'<td role="cell" class="fm-muted fm-hide-sm">{esc(row.method)}</td>'
            f'<td role="cell" class="fm-num fm-total">{esc(row.total)}</td>'
            f'<td role="cell" class="fm-status-cell">{pill}</td>'
            "</tr>"
        )
    head = (
        '<thead role="rowgroup"><tr role="row">'
        '<th role="columnheader" scope="col" class="fm-hide-sm">No</th>'
        '<th role="columnheader" scope="col">Tarih</th>'
        '<th role="columnheader" scope="col">İşletme</th>'
        '<th role="columnheader" scope="col" class="fm-hide-sm">Kategori</th>'
        '<th role="columnheader" scope="col" class="fm-hide-sm">Matrah kaynağı</th>'
        '<th role="columnheader" scope="col" class="fm-num">Toplam</th>'
        '<th role="columnheader" scope="col">Durum</th>'
        "</tr></thead>"
    )
    return (
        '<div class="fm-table-wrap"><table class="fm-table" role="table">'
        '<caption class="fm-sr">Son fişler</caption>'
        f'{head}<tbody role="rowgroup">{"".join(body)}</tbody></table></div>'
    )
