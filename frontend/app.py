"""Streamlit front-end: capture a receipt, review the OCR result, track spending.

Layout
------
* Top row     - "Fiş ekle" (live camera, file upload or bundled sample receipts) and the
                reconciliation result of the receipt that was just read.
* Second row  - the receipt image with the detected text boxes and the correction form
                (only after a receipt was read).
* Dashboard   - key figures, charts by category / month / merchant (from ``/stats``) and the
                latest receipts with filters, CSV download, merge and delete.

Look and feel: ``styles.css`` (design tokens, light and dark) and ``ui.py`` (HTML components);
the reasoning is in ``docs/DESIGN.md``. The app only talks to the FastAPI back-end
(``API_URL``, default ``http://localhost:8000``); it never runs OCR itself.
"""

import datetime as dt
import logging
import os
import re
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any, Optional, Sequence

import requests
import streamlit as st
from PIL import Image, ImageDraw

from ui import (
    BarRow,
    ColumnRow,
    Kpi,
    TableRow,
    bars_card_html,
    brand_header_html,
    card_title_html,
    charts_grid_html,
    columns_card_html,
    empty_state_html,
    kpi_grid_html,
    legend_html,
    notes_html,
    sample_text_html,
    section_title_html,
    status_card_html,
    table_html,
)

logger = logging.getLogger(__name__)

API_URL = os.getenv("API_URL", "http://localhost:8000").rstrip("/")
API_BASE = f"{API_URL}/api/v1"
EXTRACT_TIMEOUT_S = 180  # the first request may have to load the OCR model
DEFAULT_TIMEOUT_S = 15
RECENT_COUNT = 15  # rows in the "latest receipts" table
CATEGORY_LABELS = {
    "grocery": "Market",
    "food_drink": "Yeme-içme",
    "stationery": "Kırtasiye",
    "transport": "Ulaşım",
    "health": "Sağlık",
    "clothing": "Giyim",
    "electronics": "Elektronik",
    "other": "Diğer",
}
#: Shown when the server sent no explanation (after a manual correction).
METHOD_LABELS = {
    "printed": "Matrah ve KDV toplamla uyuşuyor.",
    "derived_from_rate": "Matrah KDV oranından hesaplandı ve toplamla uyuşuyor.",
    "implied_rate": "Matrah basılı değil; KDV oranı fişle uyumlu (güçlü bir kanıt değil).",
    "manual": "Girdiğiniz matrah ve KDV toplamla uyuşuyor.",
}
FLAGGED_FALLBACK = "Tutarlar uyuşmuyor ya da eksik; lütfen kontrol edin."
METHOD_SHORT = {"printed": "basılı", "derived_from_rate": "türetildi", "implied_rate": "oran uyumlu", "manual": "elle"}
ALL = "Tümü"
CHART_MONTHS = 12  # columns of the monthly chart
MONTHS_TR = ("Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara")

STYLES_PATH = Path(__file__).resolve().parent / "styles.css"
SAMPLES_DIR = Path(__file__).resolve().parent / "samples"
#: (title, file name, one-line hint)
SAMPLES: list[tuple[str, str, str]] = [
    ("Örnek Market", "ornek_market_tr.png", "Türkçe · tutarlar uyuşuyor"),
    ("Corner Cafe", "corner_cafe_en.png", "İngilizce · tutarlar uyuşuyor"),
    ("Yıldız Kırtasiye", "yildiz_kirtasiye_uyumsuz_tr.png", "Türkçe · tutarlar uyuşmuyor"),
]

CURRENCIES = ["TRY", "USD", "EUR", "GBP"]
CURRENCY_SYMBOLS = {"TRY": "₺", "USD": "$", "EUR": "€", "GBP": "£"}
VERIFIED = "verified"

CONFIDENCE_HIGH = 0.85
CONFIDENCE_MEDIUM = 0.60
# same hues as the design tokens (ok / warn / bad fills) so the legend matches the image
COLOR_HIGH = (27, 138, 78)
COLOR_MEDIUM = (217, 139, 6)
COLOR_LOW = (217, 45, 32)

_AMOUNT_RE = re.compile(
    r"(?P<int>(?!0)[0-9]{1,3}(?P<th>[.,])[0-9]{3}(?:(?P=th)[0-9]{3})*|[0-9]*)(?:(?P<dec>[.,])(?P<frac>[0-9]{1,2}))?"
)

SOURCE_CAMERA = "Kamera"
SOURCE_FILE = "Dosya"
SOURCE_SAMPLE = "Örnek fiş"
SOURCES = [SOURCE_CAMERA, SOURCE_FILE, SOURCE_SAMPLE]


class ApiError(Exception):
    """The back-end could not be reached or answered with an error."""


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------
def _error_detail(response: requests.Response) -> str:
    """Extract a readable message from an error response."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):  # FastAPI validation errors
        return "; ".join(str(item.get("msg", item)) if isinstance(item, dict) else str(item) for item in detail)
    return response.reason or "Unknown error"


def _request(method: str, path: str, *, timeout: float = DEFAULT_TIMEOUT_S, **kwargs: Any) -> requests.Response:
    """Call the API and raise :class:`ApiError` on any failure.

    Args:
        method: HTTP verb.
        path: Path below ``/api/v1``.
        timeout: Seconds before giving up.
        **kwargs: Passed to :func:`requests.request`.

    Raises:
        ApiError: Connection problem, timeout or non-2xx status.
    """
    try:
        response = requests.request(method, f"{API_BASE}{path}", timeout=timeout, **kwargs)
    except requests.ConnectionError as exc:
        raise ApiError(f"API'ye ulaşılamıyor ({API_URL}). Arka uç çalışıyor mu?") from exc
    except requests.Timeout as exc:
        raise ApiError("API zaman aşımına uğradı.") from exc
    except requests.RequestException as exc:
        raise ApiError(f"İstek başarısız: {exc}") from exc
    if not response.ok:
        detail = _error_detail(response)
        raise ApiError(detail if response.status_code < 500 else f"Sunucu hatası ({response.status_code}): {detail}")
    return response


def extract_receipt(filename: str, data: bytes, mime: str) -> dict[str, Any]:
    """``POST /extract-receipt`` - run the OCR pipeline and store the receipt."""
    response = _request(
        "POST", "/extract-receipt", timeout=EXTRACT_TIMEOUT_S, files={"file": (filename, data, mime)}
    )
    return response.json()


def fetch_image(expense_id: int) -> bytes:
    """``GET /expenses/{id}/image`` - the stored, perspective-corrected image."""
    return _request("GET", f"/expenses/{expense_id}/image").content


def update_expense(expense_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    """``PATCH /expenses/{id}`` - correct values; the status is recomputed server-side."""
    return _request("PATCH", f"/expenses/{expense_id}", json=changes).json()


def delete_expense(expense_id: int) -> None:
    """``DELETE /expenses/{id}`` - remove a receipt and its image."""
    _request("DELETE", f"/expenses/{expense_id}")


@st.cache_data(ttl=60, show_spinner=False)
def fetch_stats() -> dict[str, Any]:
    """``GET /stats`` - aggregates computed by the database."""
    return _request("GET", "/stats").json()


def _filter_params(category: Optional[str], merchant_key: Optional[str]) -> dict[str, str]:
    """Query parameters of the optional category / merchant filters."""
    params: dict[str, str] = {}
    if category:
        params["category"] = category
    if merchant_key:
        params["merchant_key"] = merchant_key
    return params


@st.cache_data(ttl=60, show_spinner=False)
def fetch_recent(category: Optional[str] = None, merchant_key: Optional[str] = None) -> list[dict[str, Any]]:
    """The newest receipts (``GET /expenses``), optionally for one category / merchant."""
    params = {"page_size": RECENT_COUNT, **_filter_params(category, merchant_key)}
    return _request("GET", "/expenses", params=params).json()["items"]


@st.cache_data(ttl=60, show_spinner=False)
def fetch_csv(category: Optional[str] = None, merchant_key: Optional[str] = None) -> bytes:
    """``GET /expenses/export.csv`` - the filtered receipts, Turkish-Excel flavour."""
    return _request("GET", "/expenses/export.csv", params=_filter_params(category, merchant_key), timeout=60).content


@st.cache_data(ttl=60, show_spinner=False)
def fetch_merchants() -> list[dict[str, Any]]:
    """``GET /merchants`` - one row per merchant (spelling variants merged)."""
    return _request("GET", "/merchants").json()


def merge_merchants(from_key: str, into_key: str) -> int:
    """``POST /merchants/merge`` - returns how many receipts moved."""
    return _request("POST", "/merchants/merge", json={"from_key": from_key, "into_key": into_key}).json()["moved"]


def refresh_data() -> None:
    """Drop the cached dashboard data (after any change)."""
    fetch_stats.clear()
    fetch_recent.clear()
    fetch_csv.clear()
    fetch_merchants.clear()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def to_decimal(value: Any) -> Optional[Decimal]:
    """Convert an API number to ``Decimal`` (``None`` stays ``None``)."""
    if value is None:
        return None
    return Decimal(str(value))


def format_number(value: Decimal) -> str:
    """Format a number Turkish style: ``Decimal('1250.5')`` -> ``1.250,50``."""
    return f"{value:,.2f}".replace(",", "\0").replace(".", ",").replace("\0", ".")


def format_int(value: int) -> str:
    """Whole number with Turkish thousands separators: ``1284`` -> ``1.284``."""
    return f"{value:,}".replace(",", ".")


def format_money(value: Optional[Decimal], currency: str) -> str:
    """Format an amount Turkish style, e.g. ``1.250,50 ₺``; ``-`` when missing."""
    if value is None:
        return "-"
    return f"{format_number(value)} {CURRENCY_SYMBOLS.get(currency, currency)}"


def format_date_tr(value: Optional[str]) -> str:
    """``2026-11-04`` -> ``04.11.2026`` (``-`` when missing, the input when malformed)."""
    if not value:
        return "-"
    try:
        return dt.date.fromisoformat(value).strftime("%d.%m.%Y")
    except ValueError:
        return value


def format_month(value: str) -> tuple[str, str]:
    """``2026-11`` -> ``("Kas", "2026")`` (the input and ``""`` when malformed)."""
    year, _, month = value.partition("-")
    if year.isdigit() and month.isdigit() and 1 <= int(month) <= 12:
        return MONTHS_TR[int(month) - 1], year
    return value, ""


def format_compact(value: Decimal) -> str:
    """Short number for chart labels: ``1312.21`` -> ``1.312``, ``5.5`` -> ``5,5`` (exact values stay in tooltips)."""
    text = f"{value:,.0f}" if abs(value) >= 100 else f"{value:,.1f}"
    text = text.replace(",", "\0").replace(".", ",").replace("\0", ".")
    return text[:-2] if text.endswith(",0") else text


def percent_text(part: Decimal, whole: Optional[Decimal]) -> str:
    """``%55`` for ``part / whole`` (empty when ``whole`` is missing or not positive)."""
    if whole is None or whole <= 0:
        return ""
    return f"%{round(100 * part / whole)}"


def parse_amount_input(text: str) -> Optional[Decimal]:
    """Parse an amount typed into the correction form.

    Accepts ``1250``, ``1250.50``, ``1250,50`` and thousands separators
    (``1.250,50``, ``1,250.50``, ``1.250``). A lone separator followed by exactly
    three digits is a thousands separator; followed by one or two digits it is
    the decimal point.

    Returns:
        The amount, or ``None`` for empty input.

    Raises:
        ValueError: If the text is not a valid amount.
    """
    match = _AMOUNT_RE.fullmatch(text.strip().replace(" ", ""))
    if not match or not (match["int"] or match["frac"]) or (match["th"] and match["th"] == match["dec"]):
        if text.strip():
            raise ValueError(f"Geçersiz tutar: {text!r} (örnek: 1250,50)")
        return None
    integer = re.sub(r"[.,]", "", match["int"])
    return Decimal(f"{integer or '0'}.{match['frac'] or '0'}").quantize(Decimal("0.01"))


def confidence_color(confidence: float) -> tuple[int, int, int]:
    """Box colour by recognition confidence (green / orange / red)."""
    if confidence >= CONFIDENCE_HIGH:
        return COLOR_HIGH
    if confidence >= CONFIDENCE_MEDIUM:
        return COLOR_MEDIUM
    return COLOR_LOW


def draw_boxes(
    image: Image.Image,
    boxes: list[dict[str, Any]],
    source_size: Optional[tuple[int, int]] = None,
) -> Image.Image:
    """Draw the detected text quadrilaterals on a copy of ``image``.

    Args:
        image: Image the boxes belong to.
        boxes: ``[{"text", "confidence", "box": [[x, y] * 4]}]`` from the API.
        source_size: ``(width, height)`` of the image the boxes were computed on;
            coordinates are scaled when it differs from ``image.size``.

    Returns:
        New RGB image with the boxes outlined.
    """
    canvas = image.convert("RGB")
    draw = ImageDraw.Draw(canvas)
    src_w, src_h = source_size or canvas.size
    scale_x, scale_y = canvas.width / src_w, canvas.height / src_h
    line_width = max(2, round(canvas.width / 350))
    for item in boxes:
        points = [(x * scale_x, y * scale_y) for x, y in item["box"]]
        draw.line([*points, points[0]], fill=confidence_color(item["confidence"]), width=line_width)
    return canvas


def category_label(value: Optional[str]) -> str:
    """Turkish display name of a category code (``None`` -> ``Diğer``)."""
    return CATEGORY_LABELS.get(value or "other", value or "other")


def bar_rows(
    rows: list[dict[str, Any]],
    currency: str,
    key: str,
    label: Any = str,
    whole: Optional[Decimal] = None,
) -> list[BarRow]:
    """Ranked-bar rows for one currency from stats rows.

    Args:
        rows: Stats rows (each has ``currency``, ``total_amount`` and ``key``).
        currency: Currency to keep.
        key: Field used as the label (``category``, ``merchant_name``).
        label: Function turning the key value into the displayed label.
        whole: The currency's grand total, used for the ``%`` share next to each value.
    """
    result: list[BarRow] = []
    for row in rows:
        if row["currency"] != currency:
            continue
        amount = to_decimal(row["total_amount"]) or Decimal("0")
        result.append(
            BarRow(label=label(row[key]), value=float(amount), text=format_money(amount, currency), share=percent_text(amount, whole))
        )
    return result


def column_rows(rows: list[dict[str, Any]], currency: str, limit: int = CHART_MONTHS) -> list[ColumnRow]:
    """Monthly columns for one currency (oldest first, at most the last ``limit`` months)."""
    picked = sorted((row for row in rows if row["currency"] == currency), key=lambda row: row["month"])[-limit:]
    result: list[ColumnRow] = []
    for row in picked:
        amount = to_decimal(row["total_amount"]) or Decimal("0")
        month, year = format_month(row["month"])
        result.append(
            ColumnRow(label=month, sublabel=year, value=float(amount), short=format_compact(amount), full=format_money(amount, currency))
        )
    return result


def takeaway(rows: Sequence[BarRow], symbol: str) -> str:
    """One-line summary of a ranked chart: ``₺ · en büyük: Market (%55)``."""
    if not rows:
        return ""
    top = rows[0]
    return f"{symbol} · en büyük: {top.label}" + (f" ({top.share})" if top.share else "")


def ledger_difference(receipt: dict[str, Any]) -> Optional[str]:
    """Signed ``total - (subtotal + tax)`` of a receipt that does not add up (``None`` otherwise)."""
    if receipt["reconciliation_status"] == VERIFIED:
        return None
    subtotal, tax, total = (to_decimal(receipt.get(field)) for field in ("subtotal_amount", "tax_amount", "total_amount"))
    if subtotal is None or tax is None or total is None:
        return None
    difference = total - (subtotal + tax)
    if difference == 0:
        return None
    return f"{'+' if difference > 0 else '−'}{format_money(abs(difference), receipt['currency'])}"


def receipt_label(item: dict[str, Any]) -> str:
    """``#7 · MIGROS · 94,00 ₺ · 02.11.2026``: identifies a receipt in a picker."""
    parts = [
        f"#{item['id']}",
        item.get("merchant_name") or "İşletme yok",
        format_money(to_decimal(item.get("total_amount")), item["currency"]),
        format_date_tr(item.get("date")),
    ]
    return " · ".join(parts)


def table_row(item: dict[str, Any]) -> TableRow:
    """One row of the latest-receipts table."""
    return TableRow(
        ident=f"#{item['id']}",
        date=format_date_tr(item.get("date")),
        merchant=item.get("merchant_name") or "İşletme okunamadı",
        category=category_label(item.get("category")),
        method=METHOD_SHORT.get(item.get("reconciliation_method") or "", "-"),
        total=format_money(to_decimal(item.get("total_amount")), item["currency"]),
        verified=item["reconciliation_status"] == VERIFIED,
    )


def build_changes(receipt: dict[str, Any], form: dict[str, Any]) -> dict[str, Any]:
    """Return only the form values that differ from ``receipt`` (PATCH body).

    Args:
        receipt: Current receipt as returned by the API.
        form: ``{"merchant_name": str, "date": date | None, "subtotal_amount": Decimal | None,
            "tax_amount": ..., "total_amount": ..., "currency": str, "category": str}``.
    """
    changes: dict[str, Any] = {}
    merchant = form["merchant_name"].strip() or None
    if merchant != receipt.get("merchant_name"):
        changes["merchant_name"] = merchant
    new_date = form["date"].isoformat() if form["date"] else None
    if new_date != receipt.get("date"):
        changes["date"] = new_date
    for field in ("subtotal_amount", "tax_amount", "total_amount"):
        if form[field] != to_decimal(receipt.get(field)):
            changes[field] = None if form[field] is None else format(form[field], "f")
    if form["currency"] != receipt["currency"]:
        changes["currency"] = form["currency"]
    if form["category"] != (receipt.get("category") or "other"):
        changes["category"] = form["category"]
    return changes


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------
def process_receipt(filename: str, data: bytes, mime: str) -> None:
    """Send an image to the API and keep the outcome in ``st.session_state['result']``."""
    st.session_state.pop("result", None)
    try:
        with st.spinner("Fiş okunuyor... (ilk istekte model yüklenir, 1-2 dk sürebilir)"):
            result = extract_receipt(filename, data, mime)
    except ApiError as exc:
        st.session_state["error"] = str(exc)
        return
    result["stored_image"] = None
    try:
        result["stored_image"] = fetch_image(result["receipt"]["id"])
    except ApiError as exc:
        logger.warning("Stored image unavailable: %s", exc)
    st.session_state.pop("error", None)
    st.session_state["result"] = result
    refresh_data()


# ---------------------------------------------------------------------------
# UI sections
# ---------------------------------------------------------------------------
def inject_styles() -> None:
    """Load ``styles.css`` (design tokens, light and dark) into the page."""
    try:
        css = STYLES_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("styles.css could not be read: %s", exc)
        return
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


def brand_icon() -> Image.Image:
    """Browser-tab icon: the brand mark (blue rounded square with a white check)."""
    size = 128
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=30, fill=(31, 79, 216, 255))
    points = [(36, 66), (56, 86), (94, 44)]
    draw.line(points, fill=(255, 255, 255, 255), width=12, joint="curve")
    for x, y in (points[0], points[-1]):
        draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=(255, 255, 255, 255))
    return image


def sticky_choice(label: str, options: Sequence[str], *, key: str, default: str, **kwargs: Any) -> str:
    """Segmented control that cannot be deselected: clicking the active segment keeps it.

    Streamlit lets a segmented control go empty; a receipt source or a currency must
    always be chosen, so the last choice is restored by the ``on_change`` callback.
    """
    last_key = f"{key}__last"
    if st.session_state.get(key) not in options:
        st.session_state.pop(key, None)  # stale value (options changed): fall back to the default

    def _restore() -> None:
        if st.session_state.get(key) is None:
            st.session_state[key] = st.session_state.get(last_key, default)
        st.session_state[last_key] = st.session_state[key]

    value = st.segmented_control(label, list(options), default=default, key=key, on_change=_restore, **kwargs)
    return value if value is not None else st.session_state.get(last_key, default)


def render_source_panel() -> None:
    """Left card: camera, file upload or sample receipts."""
    with st.container(border=True, key="card_source"):
        st.markdown(
            card_title_html("Fiş ekle", "Fotoğraf çekin, dosya yükleyin ya da örnek bir fiş deneyin."),
            unsafe_allow_html=True,
        )
        source = sticky_choice("Kaynak", SOURCES, key="source", default=SOURCE_FILE, label_visibility="collapsed")

        if source == SOURCE_CAMERA:
            photo = st.camera_input("Fişi çerçeveye yerleştirip fotoğraf çekin", label_visibility="collapsed")
            if photo is not None and st.button("Fişi oku", key="read_camera", type="primary"):
                process_receipt("camera.jpg", photo.getvalue(), photo.type or "image/jpeg")
        elif source == SOURCE_FILE:
            # No ``type=`` filter on purpose: with an extension list Android pickers hide the camera
            # option. The API rejects non-images with a clear Turkish message.
            upload = st.file_uploader("Fiş görseli", label_visibility="collapsed")
            if upload is None:
                st.caption("Telefonda bu seçenek, arka kamerayla fotoğraf çekmenizi de sağlar.")
            else:
                st.image(upload, caption=upload.name, use_container_width=True)
                if st.button("Fişi oku", key="read_upload", type="primary"):
                    process_receipt(upload.name, upload.getvalue(), upload.type or "application/octet-stream")
        else:
            found = False
            for index, (label, filename, hint) in enumerate(SAMPLES):
                path = SAMPLES_DIR / filename
                if not path.is_file():
                    continue
                found = True
                with st.container(key=f"sample_row_{index}"):
                    thumb, text, action = st.columns([1, 4, 2], vertical_alignment="center")
                    thumb.image(str(path), width=56)
                    text.markdown(sample_text_html(label, hint), unsafe_allow_html=True)
                    if action.button("Fişi oku", key=f"sample_{filename}"):
                        process_receipt(filename, path.read_bytes(), "image/png")
            if not found:
                st.warning(f"Örnek fiş bulunamadı: {SAMPLES_DIR}")


def render_result_summary() -> None:
    """Right card: reconciliation result (or an empty state that says what to do)."""
    error = st.session_state.get("error")
    if error:
        st.error(error)
    result = st.session_state.get("result")
    if result is None:
        if not error:
            with st.container(border=True, key="card_idle"):
                st.markdown(
                    empty_state_html(
                        "Henüz fiş işlenmedi",
                        "Soldan bir fiş ekleyin; okunan değerler ve mutabakat sonucu burada görünür.",
                    ),
                    unsafe_allow_html=True,
                )
        return
    if flash := st.session_state.pop("flash", None):
        st.success(flash)

    receipt = result["receipt"]
    reconciliation = result.get("reconciliation")
    currency = receipt["currency"]
    verified = receipt["reconciliation_status"] == VERIFIED
    # One short explanation: the server's message, else a label derived from the method.
    explanation = (reconciliation or {}).get("message") or (
        METHOD_LABELS.get(receipt.get("reconciliation_method") or "", "") if verified else FLAGGED_FALLBACK
    )
    who = " · ".join(
        part
        for part in (
            receipt.get("merchant_name") or "İşletme okunamadı",
            format_date_tr(receipt["date"]) if receipt.get("date") else "",
            category_label(receipt.get("category")),
        )
        if part
    )
    chips: list[str] = []
    if result.get("ocr_confidence") is not None:
        chips.append(f"OCR güveni {result['ocr_confidence']:.2f}".replace(".", ","))
    if result.get("processing_time_ms") is not None:
        chips.append(f"{result['processing_time_ms'] / 1000:.1f} sn".replace(".", ","))
    st.markdown(
        status_card_html(
            verified=verified,
            who=who,
            subtotal=format_money(to_decimal(receipt.get("subtotal_amount")), currency),
            tax=format_money(to_decimal(receipt.get("tax_amount")), currency),
            total=format_money(to_decimal(receipt.get("total_amount")), currency),
            explanation=explanation,
            difference=ledger_difference(receipt),
            chips=chips,
        ),
        unsafe_allow_html=True,
    )
    if notes := notes_html(result.get("warnings", [])):
        st.markdown(notes, unsafe_allow_html=True)


def render_annotated_image(result: dict[str, Any]) -> None:
    """Card with the stored image and the detected text boxes."""
    with st.container(border=True, key="card_image"):
        st.markdown(
            card_title_html("Okunan fiş", "Renkli kutular, OCR'ın bulduğu metin satırlarının güvenini gösterir."),
            unsafe_allow_html=True,
        )
        stored = result.get("stored_image")
        if stored is None:
            st.caption("Görsel alınamadı; kutular çizilemiyor.")
            return
        width, height = result.get("image_width"), result.get("image_height")
        source_size = (width, height) if width and height else None
        boxes = result.get("boxes", [])
        with Image.open(BytesIO(stored)) as image:
            annotated = draw_boxes(image, boxes, source_size)
        st.image(annotated, use_container_width=True)
        st.markdown(
            legend_html(len(boxes), f"{CONFIDENCE_HIGH:.2f}".replace(".", ","), f"{CONFIDENCE_MEDIUM:.2f}".replace(".", ",")),
            unsafe_allow_html=True,
        )


def render_correction_form(result: dict[str, Any]) -> None:
    """Editable form; saving sends a PATCH and refreshes the status."""
    receipt = result["receipt"]
    rid = receipt["id"]
    with st.form(f"edit_{rid}"):
        st.markdown(
            card_title_html("Değerleri düzelt", "Yanlış okunan alanı düzeltin; mutabakat yeniden hesaplanır."),
            unsafe_allow_html=True,
        )
        merchant = st.text_input("İşletme", value=receipt.get("merchant_name") or "", key=f"merchant_{rid}")
        with st.container(key=f"inline_date_{rid}"):
            col_date, col_cur = st.columns(2)
            current_date = dt.date.fromisoformat(receipt["date"]) if receipt.get("date") else None
            date_value = col_date.date_input("Tarih", value=current_date, format="DD.MM.YYYY", key=f"date_{rid}")
            currency = col_cur.selectbox(
                "Para birimi", CURRENCIES, index=CURRENCIES.index(receipt["currency"]), key=f"currency_{rid}"
            )
        categories = list(CATEGORY_LABELS)
        category = st.selectbox(
            "Kategori",
            categories,
            index=categories.index(receipt.get("category") or "other"),
            format_func=category_label,
            key=f"category_{rid}",
        )
        st.caption("Kategoriyi değiştirirseniz sistem bu işletme için hatırlar; sonraki fişleri otomatik atar.")
        with st.container(key=f"inline_amounts_{rid}"):
            col_sub, col_tax, col_total = st.columns(3)
            raw = {
                "subtotal_amount": col_sub.text_input("Matrah", value=_amount_text(receipt, "subtotal_amount"), key=f"sub_{rid}"),
                "tax_amount": col_tax.text_input("KDV", value=_amount_text(receipt, "tax_amount"), key=f"tax_{rid}"),
                "total_amount": col_total.text_input("Toplam", value=_amount_text(receipt, "total_amount"), key=f"total_{rid}"),
            }
        submitted = st.form_submit_button("Kaydet ve yeniden doğrula", type="primary")

    if not submitted:
        return
    try:
        form = {key: parse_amount_input(text) for key, text in raw.items()}
    except ValueError as exc:
        st.error(str(exc))
        return
    form.update(merchant_name=merchant, date=date_value, currency=currency, category=category)
    changes = build_changes(receipt, form)
    if not changes:
        st.info("Değişiklik yok.")
        return
    try:
        updated = update_expense(rid, changes)
    except ApiError as exc:
        st.error(str(exc))
        return
    result["receipt"] = updated
    result["reconciliation"] = None  # the PATCH response only carries the new status
    refresh_data()
    st.session_state["flash"] = "Düzeltmeler kaydedildi, mutabakat yeniden hesaplandı."
    st.rerun()


def _amount_text(receipt: dict[str, Any], field: str) -> str:
    """Plain ``1250.50`` text (two decimals) for a form field (empty when missing)."""
    value = to_decimal(receipt.get(field))
    return "" if value is None else f"{value:.2f}"


def render_result_details() -> None:
    """Second row (only after a receipt was read): the image with boxes and the correction form."""
    result = st.session_state.get("result")
    if result is None:
        return
    col_image, col_form = st.columns([1, 1.1], gap="medium")
    with col_image:
        render_annotated_image(result)
    with col_form:
        render_correction_form(result)


def render_dashboard() -> None:
    """Key figures, charts by category / month / merchant and the latest receipts."""
    head, refresh = st.columns([5, 1], vertical_alignment="center")
    head.markdown(section_title_html("Harcama özeti", "Kaydedilen tüm fişlerin özeti."), unsafe_allow_html=True)
    if refresh.button("Yenile", key="refresh_data", type="tertiary", icon=":material/refresh:"):
        refresh_data()
    try:
        stats = fetch_stats()
    except ApiError as exc:
        st.error(str(exc))
        return
    if not stats["total_receipts"]:
        with st.container(border=True, key="card_nodata"):
            st.markdown(
                empty_state_html("Henüz harcama yok", "İlk fişi ekleyin; harcama özeti burada oluşur."),
                unsafe_allow_html=True,
            )
        return

    total, verified, flagged = stats["total_receipts"], stats["verified_count"], stats["flagged_count"]
    share = round(100 * verified / total)
    kpis = [
        Kpi(
            "Fiş sayısı",
            format_int(total),
            hint=f"{verified} mutabık, {flagged} incelenecek",
            icon_name="receipt",
            ok_share=verified / total,
        ),
        Kpi("Mutabık", format_int(verified), hint=f"Fişlerin %{share}'i", tone="ok", icon_name="check"),
        Kpi("İncelenecek", format_int(flagged), hint="Kontrol bekliyor", tone="warn", icon_name="alert"),
    ]
    totals = stats["totals_by_currency"]
    for row in totals:
        cur = row["currency"]
        kpis.append(
            Kpi(
                f"Toplam ({cur})",
                format_number(to_decimal(row["total_amount"]) or Decimal("0")),
                unit=CURRENCY_SYMBOLS.get(cur, cur),
                hint=f"{row['receipt_count']} fiş · KDV {format_money(to_decimal(row['tax_amount']), cur)}",
            )
        )
    st.markdown(kpi_grid_html(kpis), unsafe_allow_html=True)

    currencies = [row["currency"] for row in totals if to_decimal(row["total_amount"])]
    if currencies:
        currency = currencies[0]
        if len(currencies) > 1:
            picker, _ = st.columns([1, 2])
            with picker:
                currency = sticky_choice("Grafik para birimi", currencies, key="chart_currency", default=currencies[0])
        symbol = CURRENCY_SYMBOLS.get(currency, currency)
        whole = next((to_decimal(row["total_amount"]) for row in totals if row["currency"] == currency), None)
        categories = bar_rows(stats["category_totals"], currency, "category", category_label, whole)
        merchants = bar_rows(stats["top_merchants"], currency, "merchant_name", str, whole)
        months = column_rows(stats["monthly_totals"], currency)
        st.markdown(
            charts_grid_html(
                [
                    bars_card_html("Kategoriye göre", takeaway(categories, symbol), categories, "Kategori verisi yok"),
                    columns_card_html("Aylık harcama", f"{symbol} cinsinden, son {CHART_MONTHS} aya kadar", months, "Tarihli fiş yok"),
                    bars_card_html("İşletmeye göre", takeaway(merchants, symbol), merchants, "İşletme verisi yok"),
                ]
            ),
            unsafe_allow_html=True,
        )
    else:
        st.info("Toplam tutarı okunmuş fiş yok; grafik çizilemiyor.")

    render_recent_table()


def render_merchant_merge(merchants: list[dict[str, Any]]) -> None:
    """Let the user say that two spellings are the same shop."""
    if len(merchants) < 2:
        st.caption("Birleştirmek için en az iki işletme gerekir.")
        return
    keys = [m["merchant_key"] for m in merchants]
    names = {m["merchant_key"]: f"{m['merchant_name']} ({m['receipt_count']})" for m in merchants}
    source = st.selectbox("Bu işletme...", keys, format_func=names.get, key="merge_from")
    target = st.selectbox("...şunun başka yazılışı", [k for k in keys if k != source], format_func=names.get, key="merge_into")
    if st.button("Birleştir", key="merge_confirm", type="primary"):
        try:
            moved = merge_merchants(source, target)
        except ApiError as exc:
            st.error(str(exc))
            return
        refresh_data()
        st.session_state["merge_flash"] = f"{moved} fiş birleştirildi."
        st.rerun()


def render_recent_table() -> None:
    """Latest receipts (filterable by category / merchant), CSV download, merge and delete."""
    with st.container(border=True, key="card_recent"):
        st.markdown(
            card_title_html("Son fişler", f"En yeni {RECENT_COUNT} fiş. Filtrelerle daraltabilirsiniz."),
            unsafe_allow_html=True,
        )
        if merge_flash := st.session_state.pop("merge_flash", None):
            st.success(merge_flash)
        try:
            merchants = fetch_merchants()
        except ApiError as exc:
            st.error(str(exc))
            return
        shop_names = {m["merchant_key"]: f"{m['merchant_name']} ({m['receipt_count']})" for m in merchants}
        col_cat, col_shop, col_csv, col_merge, col_delete = st.columns([2, 2, 1.25, 1.6, 1.1], vertical_alignment="bottom")
        category = col_cat.selectbox(
            "Kategori", [ALL, *CATEGORY_LABELS], format_func=lambda c: c if c == ALL else category_label(c), key="filter_category"
        )
        shop = col_shop.selectbox(
            "İşletme", [ALL, *shop_names], format_func=lambda k: k if k == ALL else shop_names[k], key="filter_shop"
        )
        category_filter = None if category == ALL else category
        shop_filter = None if shop == ALL else shop
        try:
            recent = fetch_recent(category_filter, shop_filter)
        except ApiError as exc:
            st.error(str(exc))
            return

        try:
            col_csv.download_button(
                "CSV indir",
                fetch_csv(category_filter, shop_filter),
                file_name="harcamalar.csv",
                mime="text/csv",
                icon=":material/download:",
                use_container_width=True,
            )
        except ApiError as exc:
            col_csv.error(str(exc))
        with col_merge.popover("İşletmeleri birleştir", icon=":material/call_merge:", use_container_width=True):
            render_merchant_merge(merchants)
        with col_delete.popover("Fiş sil", icon=":material/delete:", use_container_width=True):
            labels = {item["id"]: receipt_label(item) for item in recent}
            if not labels:
                st.caption("Silinecek fiş yok.")
            else:
                target = st.selectbox("Silinecek fiş", list(labels), format_func=labels.get, key="delete_id")
                st.caption("Bu işlem geri alınamaz.")
                if st.button("Kalıcı olarak sil", key="delete_confirm"):
                    try:
                        delete_expense(int(target))
                    except ApiError as exc:
                        st.error(str(exc))
                    else:
                        if (st.session_state.get("result") or {}).get("receipt", {}).get("id") == target:
                            st.session_state.pop("result", None)
                        refresh_data()
                        st.rerun()

        if recent:
            st.markdown(table_html([table_row(item) for item in recent]), unsafe_allow_html=True)
        else:
            st.markdown(
                empty_state_html("Bu filtreyle eşleşen fiş yok", "Filtreleri “Tümü” yaparak tüm fişleri görebilirsiniz."),
                unsafe_allow_html=True,
            )


def main() -> None:
    """Page entry point."""
    st.set_page_config(page_title="Fiş Mutabakat", page_icon=brand_icon(), layout="wide")
    inject_styles()
    st.markdown(brand_header_html(), unsafe_allow_html=True)
    left, right = st.columns([1, 1.25], gap="medium")
    with left:
        render_source_panel()
    with right:
        render_result_summary()
    render_result_details()
    render_dashboard()


if __name__ == "__main__":
    main()
