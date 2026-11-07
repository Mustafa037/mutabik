"""Receipt parser: turns an :class:`~app.services.ocr_engine.OCRResult` into fields.

Extracted fields: merchant name, date, subtotal (matrah), tax (KDV / VAT / Tax),
total, plus the detected currency and receipt language (``Locale``).

How it works
------------
* **Rows, not raw boxes.** Boxes are grouped into visual rows with
  :func:`~app.services.ocr_engine.group_into_rows`, so ``TOPLAM`` and ``106,48``
  (two boxes on one printed line) are read together.
* **Merchant** comes from the top rows of the receipt, scored by box height
  (the shop name is printed largest) after dropping address / phone / date rows.
* **Amounts** come from keyword rows. Keywords are matched on accent-folded,
  upper-cased text and tolerate small OCR errors (``TOPLAN``, ``T0PLAM``).
* **Money strings** are normalised by :func:`parse_amount`, which understands
  both ``1.250,50`` (TR) and ``1,250.50`` (EN) and returns :class:`~decimal.Decimal`
  (not ``float``) so no binary rounding error can break the reconciliation.

Nothing here validates ``subtotal + tax == total``; that is ``reconciler.py``.
"""

import datetime as dt
import difflib
import enum
import logging
import re
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final, Optional, Sequence

from app.schemas.receipt import Currency
from app.services.formatting import format_tr
from app.services.ocr_engine import OCRLine, OCRResult

__all__ = [
    "Locale",
    "ParsedReceipt",
    "parse_amount",
    "detect_locale",
    "detect_currency",
    "extract_date",
    "extract_merchant",
    "extract_amounts",
    "extract_tax_rates",
    "extract_paid_amount",
    "DERIVED_SUBTOTAL_WARNING",
    "parse_receipt",
]

logger = logging.getLogger(__name__)

#: VAT rates seen in Turkey (current: 1 / 10 / 20; older receipts: 8 / 18). Used to filter OCR noise.
KNOWN_TAX_RATES: Final[frozenset[int]] = frozenset({1, 8, 10, 18, 20})
DERIVED_SUBTOTAL_WARNING: Final[str] = "Matrah fişte basılı değil; KDV oranından hesaplandı."

MAX_AMOUNT: Final[Decimal] = Decimal("9999999999.99")  # matches Numeric(12, 2)
_CENT: Final[Decimal] = Decimal("0.01")

MERCHANT_MAX_ROWS: Final[int] = 8  # only the first rows can hold the shop name
MERCHANT_REGION_RATIO: Final[float] = 0.35  # ... and only the top 35 % of the image
MIN_DATE_YEAR: Final[int] = 2000
FUZZY_CUTOFF: Final[float] = 0.83
FUZZY_MIN_LENGTH: Final[int] = 6  # shorter words (TOTAL, TUTAR, KDV) match exactly


class Locale(str, enum.Enum):
    """Language / number-format family of a receipt."""

    TR = "tr"
    EN = "en"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ParsedReceipt:
    """Fields extracted from one receipt.

    Missing fields are ``None``; ``warnings`` explains why. Amounts are
    ``Decimal`` rounded half-up to two decimals.

    Attributes:
        merchant_name: Shop / business name.
        date: Transaction date.
        subtotal_amount: Pre-tax amount (matrah), if printed.
        tax_amount: Tax amount (total KDV / VAT / sales tax).
        total_amount: Grand total.
        currency: Detected currency; ``None`` when the receipt does not reveal it.
        locale: Detected language family.
        raw_text: OCR text in reading order (kept for auditing).
        warnings: Non-fatal remarks (missing fields, ambiguity, conflicts).
        subtotal_derived: ``True`` when ``subtotal_amount`` was rebuilt from ``KDV %rate``
            rows instead of being printed.
        tax_rates: VAT rates (in %) seen on the receipt, ascending (may be empty).
        paid_amount: What the customer actually paid (cash minus change, or the card amount),
            when the receipt shows it unambiguously; independent evidence for the total.
    """

    merchant_name: Optional[str]
    date: Optional[dt.date]
    subtotal_amount: Optional[Decimal]
    tax_amount: Optional[Decimal]
    total_amount: Optional[Decimal]
    currency: Optional[Currency]
    locale: Locale
    raw_text: str
    warnings: tuple[str, ...]
    subtotal_derived: bool = False
    tax_rates: tuple[Decimal, ...] = ()
    paid_amount: Optional[Decimal] = None


# ---------------------------------------------------------------------------
# Text normalisation helpers
# ---------------------------------------------------------------------------
_NUMERIC_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[0-9OolI|.,:/\-*]+")
_OCR_DIGIT_MAP: Final[dict[int, str]] = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1"})


def _fold(text: str) -> str:
    """Upper-case ASCII form without accents (``Şişe`` -> ``SISE``, ``İ`` -> ``I``)."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return stripped.upper()


def _fix_numeric_token(token: str) -> str:
    """Repair letter/digit confusions inside tokens that are clearly numeric.

    ``1O6,48`` -> ``106,48``; ``l2.O5.2O26`` -> ``12.05.2026``. Tokens with fewer
    than two digits (ordinary words) are left untouched.
    """
    if _NUMERIC_TOKEN_RE.fullmatch(token) and sum(ch.isdigit() for ch in token) >= 2:
        return token.translate(_OCR_DIGIT_MAP)
    return token


def _normalise_numeric_tokens(text: str) -> str:
    """Apply :func:`_fix_numeric_token` to every whitespace-separated token."""
    return " ".join(_fix_numeric_token(token) for token in text.split())


# ---------------------------------------------------------------------------
# 1. Money sanitizer
# ---------------------------------------------------------------------------
_CURRENCY_MARKER_RE: Final[re.Pattern[str]] = re.compile(r"(?i)US\$|TRY|USD|EUR|GBP|TL|[₺$€£]")
_STRIP_CHARS: Final[str] = "*=:#~ \t"


def _valid_groups(text: str, separator: str) -> bool:
    """``True`` for correct thousands grouping: ``1``-``3`` digits, then ``ddd`` groups."""
    parts = text.split(separator)
    return (
        parts[0].isdigit()
        and 1 <= len(parts[0]) <= 3
        and all(len(part) == 3 and part.isdigit() for part in parts[1:])
    )


def _split_number(number: str) -> Optional[tuple[str, str]]:
    """Split ``number`` (digits, ``.`` and ``,`` only) into integer and fraction digits.

    Rules:

    * both separators present -> the last one is the decimal separator;
    * one separator repeated -> thousands separator (``1.250.000``);
    * one separator once, exactly three digits after it (``1.250`` / ``1,250``)
      -> thousands, unless the integer part starts with ``0`` (``0,500``);
    * one separator once, one or two digits after it -> decimal separator.

    Returns:
        ``(integer_digits, fraction_digits)`` or ``None`` for malformed input.
    """
    dots, commas = number.count("."), number.count(",")
    if dots and commas:
        decimal_sep = "." if number.rfind(".") > number.rfind(",") else ","
        thousands_sep = "," if decimal_sep == "." else "."
        if number.count(decimal_sep) != 1:
            return None
        integer_part, fraction = number.split(decimal_sep)
        if len(fraction) > 2 or not _valid_groups(integer_part, thousands_sep):
            return None
        return integer_part.replace(thousands_sep, ""), fraction

    if dots or commas:
        separator = "." if dots else ","
        if max(dots, commas) > 1:
            return (number.replace(separator, ""), "") if _valid_groups(number, separator) else None
        left, right = number.split(separator)
        if len(right) == 3 and 1 <= len(left) <= 3 and not left.startswith("0"):
            return left + right, ""
        if len(right) <= 2 or (len(right) == 3 and left.startswith("0")):
            return left, right
        return None

    return number, ""


def parse_amount(text: str) -> Optional[Decimal]:
    """Convert a printed money string to a ``Decimal`` rounded to two decimals.

    Handles Turkish (``1.250,50 TL``, ``₺106,48``) and English (``$1,250.50``,
    ``1,250.50``) formats, ``*``-prefixed amounts, attached or detached currency
    markers, negatives (``-12,50``, ``12,50-``, ``(12.50)``) and common OCR
    letter/digit confusions (``1O6,48``).

    A single separator followed by exactly three digits (``1.250``, ``1,250``)
    is read as a thousands separator, since money never has three decimals.

    Args:
        text: Raw string (a single amount; not a whole line).

    Returns:
        The amount, or ``None`` if ``text`` is not a plausible monetary value
        (including values above ``MAX_AMOUNT``).
    """
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    negative = False
    if candidate.startswith("(") and candidate.endswith(")"):
        negative, candidate = True, candidate[1:-1]

    candidate = _CURRENCY_MARKER_RE.sub("", candidate).strip(_STRIP_CHARS)
    candidate = re.sub(r"\s+", "", candidate).replace("−", "-")  # drop spaces, unicode minus
    if candidate.startswith("-"):
        negative, candidate = True, candidate[1:]
    if candidate.endswith("-"):
        negative, candidate = True, candidate[:-1]
    candidate = _fix_numeric_token(candidate).strip(_STRIP_CHARS).rstrip(".,")

    if not re.fullmatch(r"\d[\d.,]*", candidate):
        return None
    parts = _split_number(candidate)
    if parts is None:
        return None
    integer_digits, fraction_digits = parts
    try:
        value = Decimal(f"{integer_digits}.{fraction_digits or '0'}").quantize(_CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    if value > MAX_AMOUNT:
        return None
    return -value if negative else value


# ---------------------------------------------------------------------------
# 2. Language and currency detection
# ---------------------------------------------------------------------------
_TR_WORDS: Final[re.Pattern[str]] = re.compile(
    r"\b(TOPLAM|KDV|TOPKDV|TARIH|NAKIT|FIS|MATRAH|ODENECEK|TUTAR|KREDI|ADET|SAAT|KASIYER|MAGAZA|"
    r"TESEKKUR\w*|URUN|INDIRIM|PARA ?USTU|VKN|MERSIS|HOSGELDINIZ|FATURA)\b"
)
_EN_WORDS: Final[re.Pattern[str]] = re.compile(
    r"\b(TOTAL|TAX|VAT|SUBTOTAL|DATE|CASH|CHANGE|AMOUNT|THANK|RECEIPT|QTY|ITEMS?|TIME|CARD|DISCOUNT|"
    r"BALANCE|CASHIER|STORE|SALES|INVOICE)\b"
)
_DATE_PATTERN: Final[str] = r"\d{1,4}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,4}"
_TR_AMOUNT_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\d.,])\d{1,3}(?:\.\d{3})*,\d{2}(?!\d)")
_EN_AMOUNT_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})*\.\d{2}(?!\d)")
_TRY_RE: Final[re.Pattern[str]] = re.compile(r"₺|(?<![A-Z])(?:TL|TRY)(?![A-Z])")
_USD_RE: Final[re.Pattern[str]] = re.compile(r"\$|(?<![A-Z])USD(?![A-Z])")
_USD_EXPLICIT_RE: Final[re.Pattern[str]] = re.compile(r"US\$|(?<![A-Z])USD(?![A-Z])")
_EUR_RE: Final[re.Pattern[str]] = re.compile(r"€|(?<![A-Z])EUR(?![A-Z])")
_GBP_RE: Final[re.Pattern[str]] = re.compile(r"£|(?<![A-Z])GBP(?![A-Z])")


def detect_locale(rows: Sequence[str]) -> Locale:
    """Guess whether a receipt is Turkish or English.

    Votes: language keywords (weight 2), amount shapes (``1.250,50`` vs
    ``1,250.50``; dates are removed first) and the ``TL`` / ``$`` markers.

    Args:
        rows: Text of each visual row.

    Returns:
        :attr:`Locale.TR`, :attr:`Locale.EN` or :attr:`Locale.UNKNOWN` on a tie.
    """
    text = _fold("\n".join(rows))
    no_dates = re.sub(_DATE_PATTERN, " ", text)
    tr_score = 2 * len(_TR_WORDS.findall(text)) + len(_TR_AMOUNT_RE.findall(no_dates)) + len(_TRY_RE.findall(text))
    # A plain "94.00" is also how Turkish e-invoice slips print money, so dot-decimal amounts
    # count only half; keywords and "1,250.50" style grouping decide.
    en_score = 2 * len(_EN_WORDS.findall(text)) + 0.5 * len(_EN_AMOUNT_RE.findall(no_dates)) + len(_USD_RE.findall(text))
    if tr_score > en_score:
        return Locale.TR
    if en_score > tr_score:
        return Locale.EN
    return Locale.UNKNOWN


def detect_currency(rows: Sequence[str], locale: Locale) -> Optional[Currency]:
    """Detect the receipt currency from symbols and codes.

    The most frequent marker wins. Without any marker a Turkish receipt is
    assumed to be in TRY; otherwise the currency is unknown (``None``).
    """
    text = _fold("\n".join(rows))
    counts = {
        Currency.TRY: len(_TRY_RE.findall(text)),
        # On Turkish receipts OCR often reads "Ş" as "$" (``$AHINTEPE``): only an explicit USD counts.
        Currency.USD: len((_USD_EXPLICIT_RE if locale is Locale.TR else _USD_RE).findall(text)),
        Currency.EUR: len(_EUR_RE.findall(text)),
        Currency.GBP: len(_GBP_RE.findall(text)),
    }
    best = max(counts.values())
    if best == 0:
        return Currency.TRY if locale is Locale.TR else None
    leaders = [currency for currency, count in counts.items() if count == best]
    if len(leaders) > 1 and locale is Locale.TR and Currency.TRY in leaders:
        return Currency.TRY
    return leaders[0]


# ---------------------------------------------------------------------------
# 3. Date extraction
# ---------------------------------------------------------------------------
_DATE_DMY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\d.,/-])(\d{1,2})\s*([./-])\s*(\d{1,2})\s*\2\s*(\d{4}|\d{2})(?!\d)"
)
_DATE_YMD_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\d.,/-])(\d{4})\s*([./-])\s*(\d{1,2})\s*\2\s*(\d{1,2})(?!\d)"
)
_DATE_KEYWORD_RE: Final[re.Pattern[str]] = re.compile(r"\b(TARIH|DATE|TAR)\b")


def extract_date(
    rows: Sequence[str],
    currency: Optional[Currency] = None,
    today: Optional[dt.date] = None,
) -> tuple[Optional[dt.date], list[str]]:
    """Find the transaction date.

    Supported: ``DD.MM.YYYY``, ``DD/MM/YYYY``, ``DD-MM-YYYY``, ``YYYY-MM-DD``
    (also ``/`` and ``.``) and two-digit years (``04.11.26``). Impossible dates
    (``31.02.2026``), years before 2000 and dates later than tomorrow are
    rejected. Rows with a ``TARIH`` / ``DATE`` keyword win, otherwise the first
    valid date from the top is used.

    When both numbers could be a month, ``.`` and ``-`` dates are read day-first
    without comment. A ``/`` date (``03/04/2026``) is read day-first, except on
    USD receipts where it is read month-first; unless the receipt is in TRY the
    guess is reported in the returned warnings.

    Args:
        rows: Text of each visual row (top to bottom).
        currency: Detected currency (resolves ambiguous ``/`` dates).
        today: Reference date for the future check (default: today).

    Returns:
        ``(date or None, warnings)``.
    """
    today = today or dt.date.today()
    latest = today + dt.timedelta(days=1)
    # (priority, row, column, (year, month, day), ambiguous)
    candidates: list[tuple[int, int, int, tuple[int, int, int], bool]] = []

    for row_index, row in enumerate(rows):
        text = _normalise_numeric_tokens(row)
        priority = 0 if _DATE_KEYWORD_RE.search(_fold(text)) else 1

        for match in _DATE_YMD_RE.finditer(text):
            year, month, day = int(match.group(1)), int(match.group(3)), int(match.group(4))
            candidates.append((priority, row_index, match.start(), (year, month, day), False))

        for match in _DATE_DMY_RE.finditer(text):
            first, separator, second = int(match.group(1)), match.group(2), int(match.group(3))
            year = int(match.group(4))
            year = year + 2000 if year < 100 else year
            ambiguous = False
            if first > 12:
                day, month = first, second
            elif second > 12:
                day, month = second, first  # unambiguous month-first (US style)
            else:
                # Dots / dashes are day-first everywhere we care about; only '/'
                # dates outside Turkey can be read either way.
                month_first = separator == "/" and currency is Currency.USD
                ambiguous = separator == "/" and first != second and currency is not Currency.TRY
                day, month = (second, first) if month_first else (first, second)
            candidates.append((priority, row_index, match.start(), (year, month, day), ambiguous))

    for _priority, _row, _column, (year, month, day), ambiguous in sorted(candidates, key=lambda item: item[:3]):
        try:
            value = dt.date(year, month, day)
        except ValueError:
            continue
        if not MIN_DATE_YEAR <= value.year <= latest.year or value > latest:
            continue
        warnings = [f"Tarih ({value.strftime('%d.%m.%Y')}) belirsiz; gün/ay sırası tahmin edildi, lütfen kontrol edin."] if ambiguous else []
        return value, warnings
    return None, []


# ---------------------------------------------------------------------------
# 4. Merchant extraction (spatial)
# ---------------------------------------------------------------------------
_MERCHANT_JUNK_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(FIS|FATURA|RECEIPT|INVOICE|TARIH|DATE|SAAT|TIME|KASA|KASIYER|CASHIER|TEL|TELEFON|PHONE|FAX|VKN|"
    r"VERGI|MERSIS|TCKN|NO|WWW|HTTPS?|EMAIL|MAIL|ADRES|ADDRESS|CAD|CADDESI|SOK|SOKAK|MAH|MAHALLESI|BLV|"
    r"BULVARI|STREET|AVENUE|AVE|ROAD|SUITE|CUSTOMER|MUSTERI|WELCOME|HOSGELDINIZ|TESEKKURLER|THANK)\b"
)
_CONTACT_RE: Final[re.Pattern[str]] = re.compile(r"@|https?:|www\.|\d{2}:\d{2}")
_LEGAL_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(r"\b(AS|LTD|STI|SAN|TIC|INC|LLC|CORP|GMBH|PLC)\b")
_BUSINESS_WORD_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(MARKET|MARKETCILIK|MAGAZACILIK|GIDA|RESTAURANT|CAFE|KAFE|ECZANE|PHARMACY|BAKKAL|FIRIN)\b"
)


def _row_text(row: Sequence[OCRLine]) -> str:
    """Join the boxes of one visual row (already left-to-right)."""
    return " ".join(line.text for line in row)


def _is_merchant_candidate(text: str) -> bool:
    """Reject rows that cannot be a shop name (address, phone, date, noise)."""
    letters = sum(ch.isalpha() for ch in text)
    visible = sum(not ch.isspace() for ch in text)
    if letters < 3 or visible == 0 or letters / visible < 0.6:
        return False
    folded = _fold(text)
    if _MERCHANT_JUNK_RE.search(folded) or _CONTACT_RE.search(text):
        return False
    return not (_DATE_DMY_RE.search(text) or _DATE_YMD_RE.search(text))


def _clean_merchant(text: str) -> Optional[str]:
    """Trim decoration (``***``, dashes) and collapse whitespace."""
    cleaned = re.sub(r"^[^\w]+|[^\w.]+$", "", " ".join(text.split()))
    return cleaned[:255].rstrip() if len(cleaned) >= 2 else None


def extract_merchant(rows: Sequence[Sequence[OCRLine]], image_height: float) -> Optional[str]:
    """Pick the shop name from the top rows by box height.

    Among the first ``MERCHANT_MAX_ROWS`` rows lying in the top
    ``MERCHANT_REGION_RATIO`` of the image, the tallest eligible row wins (shop
    names are printed largest); legal-entity words (``A.Ş.``, ``LTD``, ``MARKET``)
    add a bonus and earlier rows win ties. A directly following row that only
    holds the legal suffix (``TİC. A.Ş.``) is appended.

    Args:
        rows: Visual rows from top to bottom (see ``group_into_rows``).
        image_height: Height of the OCR image in pixels.

    Returns:
        The merchant name or ``None`` if no row qualifies.
    """
    if not rows:
        return None
    heights = [max(line.height for line in row) for row in rows]
    reference = sorted(heights)[len(heights) // 2] or 1.0

    def center_y(row: Sequence[OCRLine]) -> float:
        return sum(line.y_center for line in row) / len(row)

    head = list(range(min(MERCHANT_MAX_ROWS, len(rows))))
    in_region = [i for i in head if center_y(rows[i]) <= MERCHANT_REGION_RATIO * image_height]
    eligible = [i for i in in_region if _is_merchant_candidate(_row_text(rows[i]))]
    if not eligible:  # header rows are all junk: widen to the first rows of the receipt
        eligible = [i for i in head if _is_merchant_candidate(_row_text(rows[i]))]
    if not eligible:
        return None

    def score(index: int) -> float:
        folded = _fold(_row_text(rows[index])).replace(".", "")
        bonus = 0.5 if (_LEGAL_SUFFIX_RE.search(folded) or _BUSINESS_WORD_RE.search(folded)) else 0.0
        return heights[index] / reference + bonus - 0.05 * index

    best = max(eligible, key=score)
    name = _row_text(rows[best])
    following = best + 1
    if following < min(MERCHANT_MAX_ROWS, len(rows)) and _is_merchant_candidate(_row_text(rows[following])):
        next_folded = _fold(_row_text(rows[following])).replace(".", "")
        current_folded = _fold(name).replace(".", "")
        if _LEGAL_SUFFIX_RE.search(next_folded) and not _LEGAL_SUFFIX_RE.search(current_folded):
            name = f"{name} {_row_text(rows[following])}"
    return _clean_merchant(name)


# ---------------------------------------------------------------------------
# 5. Amount rows: total / tax / subtotal
# ---------------------------------------------------------------------------
_EXCLUDE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(NAKIT|CASH|KREDI|KART|KARTI|CARD|VISA|MASTER\w*|PARA ?USTU|CHANGE|TENDER\w*|ODEME|PAYMENT|"
    r"INDIRIM|DISCOUNT|ADET|QTY|ITEMS?|KALEM|TIPS?|BAHSIS|YUVARLAMA|ROUND\w*|PUAN|POINTS?|KUPON|COUPON)\b"
)
_SUBTOTAL_STRONG_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(ARA ?TOPLAM|SUB ?TOTAL|TOPLAM MATRAH|NET ?TOPLAM|NET TOTAL|NET TUTAR|NET AMOUNT|KDV ?HARIC|KDVSIZ|"
    r"EXCL\w* (TAX|VAT)|BEFORE (TAX|VAT)|PRE ?TAX)\b"
)
_SUBTOTAL_WEAK_RE: Final[re.Pattern[str]] = re.compile(r"\b(MATRAH\w*|TAXABLE)\b")
_TOTAL_STRONG_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(GENEL ?TOPLAM|GRAND ?TOTAL|TOTAL (DUE|AMOUNT|PAYABLE|TO PAY)|(AMOUNT|BALANCE) DUE|ODENECEK|"
    r"TOPLAM TUTAR|KDV DAHIL|INCL\w* (TAX|VAT))\b"
)
_TAX_STRONG_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(TOP ?KDV|TOPLAM KDV|TOTAL (SALES )?(TAX|VAT)|TAX TOTAL|VAT TOTAL)\b"
)
_TAX_WEAK_RE: Final[re.Pattern[str]] = re.compile(r"\b(KDV|VAT|TAX|HST|GST|PST|VERGI|MWST)\b")
_TOTAL_WEAK_RE: Final[re.Pattern[str]] = re.compile(r"\b(TOPLAM|TOTAL|TUTAR|AMOUNT)\b")

_FUZZY_VOCABULARY: Final[tuple[str, ...]] = (
    "TOPLAM", "TOPKDV", "SUBTOTAL", "MATRAH", "ODENECEK", "INDIRIM", "DISCOUNT", "PAYMENT", "BALANCE", "TENDER", "GENEL",
)

DERIVATION_TAX_TOLERANCE: Final[Decimal] = Decimal("0.02")
TAX_FIT_TOLERANCE: Final[Decimal] = Decimal("0.10")  # per-line rounding; same as the reconciler's implied-rate check
_TAX_RATE_RE: Final[re.Pattern[str]] = re.compile(r"%\s*(\d{1,2})(?!\d)")
_SPLIT_DECIMAL_RE: Final[re.Pattern[str]] = re.compile(r"(?<=\d)\s+(?=[.,]\d{2}\b)|(?<=\d[.,])\s+(?=\d{2}\b)")


def _keyword_text(text: str) -> str:
    """Folded, punctuation-free text for keyword matching, with fuzzy word repair."""
    folded = re.sub(r"(?<=[A-Z])0|0(?=[A-Z])", "O", _fold(text))  # T0PLAM -> TOPLAM
    folded = re.sub(r"['`’.]", "", folded)
    words: list[str] = []
    for word in re.sub(r"[^A-Z0-9]+", " ", folded).split():
        if len(word) >= FUZZY_MIN_LENGTH and word not in _FUZZY_VOCABULARY and not any(c.isdigit() for c in word):
            close = difflib.get_close_matches(word, _FUZZY_VOCABULARY, n=1, cutoff=FUZZY_CUTOFF)
            word = close[0] if close else word
        words.append(word)
    return " ".join(words)


def _classify_row(text: str) -> Optional[tuple[str, bool]]:
    """Classify a row as ``subtotal`` / ``total`` / ``tax`` and tell if it is *strong*.

    Returns ``None`` for payment, change, discount and item-count rows as well as
    for rows without any keyword. Order matters: ``KDV DAHIL TOPLAM`` is a total,
    ``TOPLAM KDV`` is tax, ``ARA TOPLAM`` is a subtotal.
    """
    keywords = _keyword_text(text)
    if _EXCLUDE_RE.search(keywords):
        return None
    if _SUBTOTAL_STRONG_RE.search(keywords):
        return "subtotal", True
    if _SUBTOTAL_WEAK_RE.search(keywords):
        return "subtotal", False
    if _TOTAL_STRONG_RE.search(keywords):
        return "total", True
    if _TAX_STRONG_RE.search(keywords):
        return "tax", True
    if _TAX_WEAK_RE.search(keywords):
        return "tax", False
    if _TOTAL_WEAK_RE.search(keywords):
        return "total", False
    return None


def _row_amounts(text: str) -> list[Decimal]:
    """All monetary values in a row, left to right.

    A token counts as money when it has one or two decimals (``9,68``) or an
    explicit currency marker (``110 TL``). Percentages (``%10``), barcodes, dates
    and plain integers are ignored.
    """
    joined = _SPLIT_DECIMAL_RE.sub("", _normalise_numeric_tokens(text))  # "106, 48" -> "106,48"
    amounts: list[Decimal] = []
    for token in joined.split():
        if "%" in token:
            continue
        has_marker = bool(_CURRENCY_MARKER_RE.search(token)) and any(ch.isdigit() for ch in token)
        has_decimals = bool(re.search(r"\d[.,]\d{1,2}\)?-?$", _CURRENCY_MARKER_RE.sub("", token).strip(_STRIP_CHARS)))
        if not (has_marker or has_decimals):
            continue
        value = parse_amount(token)
        if value is not None:
            amounts.append(value)
    return amounts


_RATE_PERCENT_BEFORE_RE: Final[re.Pattern[str]] = re.compile(r"%\s*0?(\d{1,2})(?!\d)")
_RATE_PERCENT_AFTER_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\d.,])0?(\d{1,2})\s*%")
_RATE_DOLLAR_RE: Final[re.Pattern[str]] = re.compile(r"\$\s?0?(\d{1,2})(?![\d.,])")  # OCR reads "%" as "$"


def extract_tax_rates(rows: Sequence[str], dollar_is_percent: bool = False) -> tuple[Decimal, ...]:
    """VAT rates (in %) printed anywhere on the receipt.

    Turkish till receipts usually put the rate at the end of every item line
    (``EKMEK %01 *12,50``), not only on a ``KDV %10`` summary row. Only real VAT
    rates (:data:`KNOWN_TAX_RATES`) are kept, which filters most OCR noise; discount,
    cash and change rows are skipped.

    Args:
        rows: Text of each visual row.
        dollar_is_percent: Also read ``$20`` as ``%20`` (OCR confusion; Turkish receipts only).

    Returns:
        Distinct rates in ascending order.
    """
    patterns = [_RATE_PERCENT_BEFORE_RE, _RATE_PERCENT_AFTER_RE] + ([_RATE_DOLLAR_RE] if dollar_is_percent else [])
    seen: set[int] = set()
    for row in rows:
        if _EXCLUDE_RE.search(_keyword_text(row)):
            continue
        for pattern in patterns:
            seen.update(int(match.group(1)) for match in pattern.finditer(row))
    return tuple(Decimal(rate) for rate in sorted(seen & KNOWN_TAX_RATES))


_CASH_RE: Final[re.Pattern[str]] = re.compile(r"\b(NAKIT|CASH)\b")
_CHANGE_RE: Final[re.Pattern[str]] = re.compile(r"\b(PARA ?USTU|CHANGE)\b")
_CARD_RE: Final[re.Pattern[str]] = re.compile(r"\b(KREDI|KART|KARTI|CARD|VISA|MASTER\w*)\b")


def extract_paid_amount(rows: Sequence[str]) -> Optional[Decimal]:
    """What was actually paid, read from the payment rows (independent of the ``TOPLAM`` row).

    * cash and change present -> ``cash - change``;
    * only a card row -> the card amount;
    * anything else (cash alone, cash *and* card, no payment rows) -> ``None``: a lone cash
      amount can exceed the bill and split payments are ambiguous, so they prove nothing.

    Args:
        rows: Text of each visual row.
    """
    cash = change = card = None
    for row in rows:
        amounts = _row_amounts(row)
        if not amounts:
            continue
        keywords = _keyword_text(row)
        value = abs(amounts[-1])
        if _CHANGE_RE.search(keywords):
            change = value
        elif _CASH_RE.search(keywords):
            cash = value
        elif _CARD_RE.search(keywords):
            card = value
    if card is not None and cash is None:
        return card
    if card is None and cash is not None and change is not None and cash > change:
        return cash - change
    return None


def _strip_leading_digit(value: Decimal) -> Optional[Decimal]:
    """``494.00`` -> ``94.00`` (drop one leading digit); ``None`` for single-digit integers."""
    integer, _, fraction = f"{value:.2f}".partition(".")
    if len(integer) < 2:
        return None
    return Decimal(f"{integer[1:]}.{fraction}").quantize(_CENT)


def _repair_total(
    total: Decimal,
    tax: Optional[Decimal],
    subtotal: Optional[Decimal],
    paid: Optional[Decimal],
    rates: Sequence[Decimal],
) -> tuple[Decimal, Optional[str]]:
    """Undo a misread ``*`` prefix that turned ``*94.00`` into ``494.00``.

    Till receipts print amounts as ``*94,00``; OCR sometimes reads the star as a digit.
    The candidate (the total without its first digit) replaces the printed value only if
    independent evidence supports it and nothing supports the printed one:

    * the amount paid (cash - change / card),
    * ``subtotal + tax == total``,
    * the tax fits one VAT rate (``tax ~ total * r / (100 + r)``).

    Returns:
        ``(total, warning)``; ``warning`` is set when the value was replaced.
    """
    candidate = _strip_leading_digit(total)
    if candidate is None:
        return total, None
    candidate_rates = [int(rate) for rate in rates] or sorted(KNOWN_TAX_RATES)

    def support(value: Decimal) -> int:
        score = 0
        if paid is not None and abs(paid - value) <= DERIVATION_TAX_TOLERANCE:
            score += 1
        if subtotal is not None and tax is not None and abs(subtotal + tax - value) <= DERIVATION_TAX_TOLERANCE:
            score += 1
        if tax is not None and any(abs(tax - value * rate / (100 + rate)) <= TAX_FIT_TOLERANCE for rate in candidate_rates):
            score += 1
        return score

    if support(total) == 0 and support(candidate) >= 1:
        return candidate, (
            f"Toplam {format_tr(total)} yerine {format_tr(candidate)} olarak düzeltildi: ödeme ve KDV bu değerle "
            "uyumlu (baştaki '*' işareti rakam okunmuş olabilir). Lütfen yine de kontrol edin."
        )
    return total, None


def extract_amounts(rows: Sequence[str]) -> tuple[dict[str, Optional[Decimal]], list[str]]:
    """Extract total, tax and subtotal from keyword rows.

    * Every row is classified (see :func:`_classify_row`) and its **right-most**
      amount is taken. If a keyword row has no amount, an amount-only row right
      below it is used (values printed on the next line).
    * **total**: strong keyword (``GENEL TOPLAM``, ``ÖDENECEK TUTAR``, ``GRAND
      TOTAL``, ``KDV DAHİL``) beats weak (``TOPLAM``, ``TOTAL``); the last match
      in reading order wins.
    * **tax** / **subtotal**: a strong row (``TOPKDV``, ``TOTAL TAX``, ``ARA
      TOPLAM``) is used as is; otherwise the per-rate rows (``KDV %1``, ``KDV
      %10``, ``MATRAH``) are summed.

    Args:
        rows: Text of each visual row (top to bottom).

    Returns:
        ``({"total": ..., "tax": ..., "subtotal": ...}, warnings)``; missing
        values are ``None``.
    """
    found: dict[str, list[tuple[bool, Decimal]]] = {"total": [], "tax": [], "subtotal": []}
    tax_rows: list[tuple[Decimal, Optional[Decimal]]] = []  # (amount, rate in % or None) of weak tax rows
    for index, row in enumerate(rows):
        classification = _classify_row(row)
        if classification is None:
            continue
        kind, strong = classification
        amounts = _row_amounts(row)
        if kind == "subtotal" and not strong and len(amounts) >= 2 and _TAX_WEAK_RE.search(_keyword_text(row)):
            # "KDV MATRAHI 96,80  KDV 17,42": two columns on one row.
            found["subtotal"].append((False, amounts[0]))
            found["tax"].append((False, amounts[-1]))
            continue
        if not amounts and index + 1 < len(rows):
            below = rows[index + 1]
            below_amounts = _row_amounts(below)
            if len(below_amounts) == 1 and _classify_row(below) is None:
                amounts = below_amounts
        if amounts:
            found[kind].append((strong, amounts[-1]))
            if kind == "tax" and not strong:
                rate_match = _TAX_RATE_RE.search(row)
                tax_rows.append((amounts[-1], Decimal(rate_match.group(1)) if rate_match else None))

    warnings: list[str] = []

    def last_of_best(items: list[tuple[bool, Decimal]]) -> Optional[Decimal]:
        strong_items = [value for strong, value in items if strong]
        pool = strong_items or [value for _, value in items]
        if len(set(strong_items)) > 1:
            warnings.append("Fişte birbirinden farklı birden fazla toplam bulundu; sonuncusu kullanıldı, lütfen kontrol edin.")
        return pool[-1] if pool else None

    def strong_or_sum(items: list[tuple[bool, Decimal]]) -> Optional[Decimal]:
        strong_items = [value for strong, value in items if strong]
        if strong_items:
            return strong_items[-1]
        weak_items = [value for _, value in items]
        return sum(weak_items, Decimal("0")) if weak_items else None

    total = last_of_best(found["total"])
    tax = strong_or_sum(found["tax"])
    subtotal = strong_or_sum(found["subtotal"])
    strong_tax_printed = any(strong for strong, _ in found["tax"])
    if subtotal is None and tax_rows and all(rate is not None and rate > 0 for _, rate in tax_rows):
        rows_tax = sum((amount for amount, _ in tax_rows), Decimal("0"))
        # A printed TOPKDV must agree with the per-rate rows, otherwise something was misread.
        if not strong_tax_printed or (tax is not None and abs(rows_tax - tax) <= DERIVATION_TAX_TOLERANCE):
            # Matrah is not printed: rebuild it from "KDV %rate amount" rows. The result is
            # independent of the total, so the reconciliation stays a real cross-check.
            subtotal = sum(
                ((amount * 100 / rate).quantize(_CENT, rounding=ROUND_HALF_UP) for amount, rate in tax_rows if rate),
                Decimal("0"),
            )
            warnings.append(DERIVED_SUBTOTAL_WARNING)
    if total is not None and tax is not None and abs(tax) > abs(total):
        warnings.append("KDV tutarı toplamdan büyük görünüyor; bir rakam yanlış okunmuş olabilir.")
    return {"total": total, "tax": tax, "subtotal": subtotal}, warnings


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def parse_receipt(ocr: OCRResult, today: Optional[dt.date] = None) -> ParsedReceipt:
    """Extract all receipt fields from an OCR result.

    Args:
        ocr: Result of :meth:`OCREngine.recognize`.
        today: Reference date for date validation (tests); default: today.

    Returns:
        A :class:`ParsedReceipt`. Fields that could not be found are ``None`` and
        explained in ``warnings``; this function does not raise on poor OCR.
    """
    rows = ocr.rows
    texts = [_row_text(row) for row in rows]
    warnings: list[str] = []
    if not texts:
        return ParsedReceipt(None, None, None, None, None, None, Locale.UNKNOWN, "", ("Fişte metin bulunamadı.",))

    locale = detect_locale(texts)
    currency = detect_currency(texts, locale)
    merchant = extract_merchant(rows, float(ocr.image_height))
    receipt_date, date_warnings = extract_date(texts, currency, today)
    amounts, amount_warnings = extract_amounts(texts)
    tax_rates = extract_tax_rates(texts, dollar_is_percent=locale is Locale.TR)
    paid_amount = extract_paid_amount(texts)
    if amounts["total"] is not None:
        amounts["total"], repair_warning = _repair_total(
            amounts["total"], amounts["tax"], amounts["subtotal"], paid_amount, tax_rates
        )
        if repair_warning:
            amount_warnings.append(repair_warning)
    warnings.extend(date_warnings)
    # the "matrah was derived" note is already part of the reconciliation message
    warnings.extend(warning for warning in amount_warnings if warning != DERIVED_SUBTOTAL_WARNING)

    if merchant is None:
        warnings.append("İşletme adı okunamadı; lütfen elle girin.")
    if receipt_date is None:
        warnings.append("Tarih okunamadı; lütfen elle girin.")
    if amounts["total"] is None:
        warnings.append("Toplam tutar okunamadı; fişin tamamı kadraja girecek ve net olacak şekilde tekrar çekin.")

    logger.debug("Parsed receipt: locale=%s currency=%s warnings=%d", locale.value, currency, len(warnings))
    return ParsedReceipt(
        merchant_name=merchant,
        date=receipt_date,
        subtotal_amount=amounts["subtotal"],
        tax_amount=amounts["tax"],
        total_amount=amounts["total"],
        currency=currency,
        locale=locale,
        raw_text=ocr.text,
        warnings=tuple(warnings),
        subtotal_derived=DERIVED_SUBTOTAL_WARNING in amount_warnings,
        tax_rates=tax_rates,
        paid_amount=paid_amount,
    )
