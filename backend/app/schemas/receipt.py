"""Pydantic schemas (request / response contracts) for the receipt API.

This module is intentionally free of any SQLAlchemy import so that the parser,
reconciler and API layers can depend on it without touching the database layer.
The shared enums (:class:`Currency`, :class:`ReconciliationStatus`) live here and
are re-used by ``app.db.models``.

Monetary values
---------------
Amounts are modelled as :class:`decimal.Decimal` rounded half-up to two
decimals (``Money``). In JSON responses they are serialised as plain numbers so
that the Streamlit / pandas front-end can consume them without conversions;
in Python (``model_dump()``) they stay ``Decimal``.
"""

import datetime as dt
import enum
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any, Final, Literal, Optional

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    computed_field,
    field_validator,
    model_validator,
)

__all__ = [
    "Currency",
    "Category",
    "ReconciliationStatus",
    "Money",
    "ReceiptBase",
    "ReceiptCreate",
    "ReceiptUpdate",
    "ReceiptSummary",
    "ReceiptRead",
    "ExpenseFilter",
    "ReceiptListQuery",
    "ExportQuery",
    "ReceiptListResponse",
    "ReconciliationDetail",
    "OCRBox",
    "ReceiptProcessResponse",
    "CurrencyTotal",
    "MonthlyTotal",
    "CategoryTotal",
    "MerchantTotal",
    "MerchantInfo",
    "StatsResponse",
    "HealthResponse",
    "ErrorResponse",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class Currency(str, enum.Enum):
    """ISO-4217 currency codes supported by the system."""

    TRY = "TRY"
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"


class Category(str, enum.Enum):
    """Spending category, guessed from the merchant / items and editable by the user."""

    GROCERY = "grocery"
    FOOD_DRINK = "food_drink"
    STATIONERY = "stationery"
    TRANSPORT = "transport"
    HEALTH = "health"
    CLOTHING = "clothing"
    ELECTRONICS = "electronics"
    OTHER = "other"


class ReconciliationStatus(str, enum.Enum):
    """Outcome of the ``subtotal + tax == total`` consistency check.

    ``FLAGGED_FOR_REVIEW`` covers both a failed check and missing amounts; the
    reason is carried by ``ReconciliationDetail.message``.
    """

    VERIFIED = "verified"
    FLAGGED_FOR_REVIEW = "flagged_for_review"


#: Symbols / local abbreviations that may reach the API and map to an ISO code.
CURRENCY_ALIASES: Final[dict[str, str]] = {
    "TL": "TRY",
    "₺": "TRY",
    "$": "USD",
    "US$": "USD",
    "€": "EUR",
    "£": "GBP",
}

_CENT: Final[Decimal] = Decimal("0.01")
_MERCHANT_MAX_LENGTH: Final[int] = 255
_IMAGE_PATH_MAX_LENGTH: Final[int] = 512


# ---------------------------------------------------------------------------
# Reusable validated types
# ---------------------------------------------------------------------------
def _to_money(value: Any) -> Any:
    """Coerce ``value`` to a ``Decimal`` rounded half-up to two decimals.

    Accepts ``Decimal``, ``int``, ``float`` and plain numeric strings such as
    ``"1234.50"``. Locale formatted strings (``"1.234,50"``) are rejected on
    purpose: normalising them is the parser/sanitizer's job.

    Args:
        value: Raw input value.

    Returns:
        The quantized ``Decimal``, or ``None`` when ``value`` is ``None``.

    Raises:
        ValueError: If the value is not a finite number.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Amount must be a number, not a boolean.")
    try:
        if isinstance(value, Decimal):
            number = value
        elif isinstance(value, int):
            number = Decimal(value)
        elif isinstance(value, str):
            number = Decimal(value.strip())
        elif isinstance(value, float):
            # repr() gives the shortest round-trip form and avoids binary noise.
            number = Decimal(repr(value))
        else:
            raise ValueError(f"Unsupported amount type: {type(value).__name__}.")
        if not number.is_finite():
            raise ValueError("Amount must be a finite number.")
        return number.quantize(_CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid monetary amount: {value!r}.") from exc


def _money_to_float(value: Decimal) -> float:
    """Serialise a ``Decimal`` amount as a JSON number."""
    return float(value)


#: Two-decimal monetary amount, at most 12 digits in total (matches the DB column).
#: NOTE: the ``Field`` constraints must come *before* ``BeforeValidator``; when
#: placed after it, Pydantic silently skips ``max_digits`` / ``decimal_places``.
Money = Annotated[
    Decimal,
    Field(max_digits=12, decimal_places=2),
    BeforeValidator(_to_money),
    PlainSerializer(_money_to_float, return_type=float, when_used="json"),
]


# ---------------------------------------------------------------------------
# Core receipt schemas
# ---------------------------------------------------------------------------
class ReceiptBase(BaseModel):
    """Fields shared by every receipt schema.

    All business fields are optional because OCR can fail to extract any of
    them; missing data is reported by the reconciliation status rather than by
    rejecting the payload. ``subtotal_amount`` is the pre-tax amount
    (Turkish: *matrah*).
    """

    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)

    merchant_name: Optional[str] = Field(
        default=None,
        max_length=_MERCHANT_MAX_LENGTH,
        description="Merchant / business name as read from the receipt.",
        examples=["MIGROS TICARET A.S."],
    )
    date: Optional[dt.date] = Field(
        default=None,
        description="Transaction date (ISO 8601).",
        examples=["2026-11-04"],
    )
    subtotal_amount: Optional[Money] = Field(
        default=None,
        description="Pre-tax amount (matrah).",
        examples=[100.00],
    )
    tax_amount: Optional[Money] = Field(
        default=None,
        description="Total tax (KDV / VAT / sales tax).",
        examples=[20.00],
    )
    total_amount: Optional[Money] = Field(
        default=None,
        description="Grand total paid.",
        examples=[120.00],
    )
    currency: Currency = Field(
        default=Currency.TRY,
        description="ISO-4217 currency code. Defaults to TRY when undetected.",
    )
    category: Optional[Category] = Field(
        default=None,
        description="Spending category (guessed from the merchant; ``null`` on old rows).",
    )

    @field_validator("merchant_name", mode="before")
    @classmethod
    def _clean_merchant_name(cls, value: Any) -> Any:
        """Collapse whitespace, truncate to the column size, blank -> ``None``."""
        if isinstance(value, str):
            cleaned = " ".join(value.split())
            return cleaned[:_MERCHANT_MAX_LENGTH].rstrip() or None
        return value

    @field_validator("currency", mode="before")
    @classmethod
    def _normalise_currency(cls, value: Any) -> Any:
        """Accept case-insensitive codes and common symbols (``TL``, ``$``, ...)."""
        if isinstance(value, str):
            token = value.strip()
            upper = token.upper()
            return CURRENCY_ALIASES.get(token, CURRENCY_ALIASES.get(upper, upper))
        return value


class ReceiptCreate(ReceiptBase):
    """Payload used internally to persist a processed receipt."""

    merchant_key: Optional[str] = Field(default=None, max_length=_MERCHANT_MAX_LENGTH)

    reconciliation_status: ReconciliationStatus = Field(
        default=ReconciliationStatus.FLAGGED_FOR_REVIEW,
        description="Result of the subtotal + tax == total check.",
    )
    reconciliation_method: Optional[str] = Field(
        default=None,
        description="How the subtotal was obtained: printed, derived_from_rate, "
        "implied_rate or manual (corrected by the user); null when there is no subtotal.",
    )
    raw_text: Optional[str] = Field(
        default=None,
        description="Full OCR text (reading order) kept for auditing / debugging.",
    )
    image_path: Optional[str] = Field(
        default=None,
        max_length=_IMAGE_PATH_MAX_LENGTH,
        description="Location of the stored receipt image.",
    )


class ReceiptUpdate(ReceiptBase):
    """Partial update (``PATCH``) used to correct wrongly extracted values.

    Only fields present in the request body are applied (use
    ``model_dump(exclude_unset=True)``). An explicit ``null`` clears an optional
    value; ``currency`` cannot be cleared. Reconciliation status is not editable
    here: it is recomputed from the corrected amounts.
    """

    currency: Optional[Currency] = Field(  # type: ignore[assignment]
        default=None,
        description="ISO-4217 currency code.",
    )

    @model_validator(mode="after")
    def _validate_update(self) -> "ReceiptUpdate":
        """Require at least one field and forbid ``currency: null``."""
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided for an update.")
        if "currency" in self.model_fields_set and self.currency is None:
            raise ValueError("currency cannot be null.")
        return self


class ReceiptSummary(ReceiptBase):
    """Lightweight receipt representation used in list responses (no OCR text)."""

    id: int = Field(description="Primary key.")
    merchant_key: Optional[str] = Field(
        default=None, description="Normalised merchant identity; spelling variants share one key."
    )
    reconciliation_status: ReconciliationStatus
    reconciliation_method: Optional[str] = Field(
        default=None,
        description="How the subtotal was obtained: printed, derived_from_rate, "
        "implied_rate or manual (corrected by the user); null when there is no subtotal.",
    )
    image_path: Optional[str] = None
    created_at: dt.datetime = Field(description="Creation timestamp (UTC).")


class ReceiptRead(ReceiptSummary):
    """Full receipt representation, including the raw OCR text."""

    raw_text: Optional[str] = None


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
class ExpenseFilter(BaseModel):
    """Filters shared by the list and export endpoints (query-string parameters)."""

    model_config = ConfigDict(extra="forbid")

    merchant: Optional[str] = Field(
        default=None,
        max_length=_MERCHANT_MAX_LENGTH,
        description="Case-insensitive substring filter on the merchant name.",
    )
    merchant_key: Optional[str] = Field(
        default=None, max_length=_MERCHANT_MAX_LENGTH, description="Exact merchant identity (see ``GET /merchants``)."
    )
    currency: Optional[Currency] = None
    status: Optional[ReconciliationStatus] = None
    category: Optional[Category] = None
    date_from: Optional[dt.date] = Field(default=None, description="Inclusive lower bound.")
    date_to: Optional[dt.date] = Field(default=None, description="Inclusive upper bound.")

    @model_validator(mode="after")
    def _validate_date_range(self) -> "ExpenseFilter":
        """Ensure ``date_from <= date_to`` when both bounds are given."""
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to.")
        return self


class ReceiptListQuery(ExpenseFilter):
    """Query-string parameters for ``GET /expenses``.

    Usable as ``Annotated[ReceiptListQuery, Query()]`` (FastAPI >= 0.115).
    """

    page: int = Field(default=1, ge=1, description="1-based page number.")
    page_size: int = Field(default=20, ge=1, le=100, description="Items per page.")

    @property
    def offset(self) -> int:
        """Number of rows to skip for the requested page."""
        return (self.page - 1) * self.page_size


class ExportQuery(ExpenseFilter):
    """Query-string parameters for ``GET /expenses/export.csv``."""

    locale: Literal["tr", "en"] = Field(
        default="tr",
        description="``tr``: ';' separator and decimal comma (Turkish Excel); ``en``: ',' and decimal point.",
    )


class ReceiptListResponse(BaseModel):
    """Paginated list of receipts."""

    items: list[ReceiptSummary]
    total: int = Field(ge=0, description="Total rows matching the filters.")
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_pages(self) -> int:
        """Number of pages for ``total`` rows (0 when there are no rows)."""
        return (self.total + self.page_size - 1) // self.page_size


# ---------------------------------------------------------------------------
# Processing (upload -> OCR -> parse -> reconcile)
# ---------------------------------------------------------------------------
class ReconciliationDetail(BaseModel):
    """Explains how the ``subtotal + tax == total`` check came out."""

    status: ReconciliationStatus
    subtotal_amount: Optional[Money] = None
    tax_amount: Optional[Money] = None
    total_amount: Optional[Money] = None
    expected_total: Optional[Money] = Field(
        default=None,
        description="subtotal + tax, when both are available.",
    )
    difference: Optional[Money] = Field(
        default=None,
        description="total - expected_total (signed).",
    )
    tolerance: Money = Field(
        default=Decimal("0.01"),
        description="Absolute difference still considered a match.",
    )
    payment_amount: Optional[Money] = Field(
        default=None, description="What was paid (cash - change, or the card amount), when the receipt shows it."
    )
    payment_matches: Optional[bool] = Field(
        default=None, description="Whether the payment agrees with the total (``null`` without payment evidence)."
    )
    method: Optional[str] = Field(
        default=None,
        description="How the subtotal was obtained (printed, derived_from_rate, implied_rate, manual); null when it could not be verified.",
    )
    message: str = Field(description="Human-readable explanation.")


class OCRBox(BaseModel):
    """One detected text line with its quadrilateral (pixels of the stored image)."""

    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    box: list[tuple[float, float]] = Field(
        min_length=4,
        max_length=4,
        description="Corner points TL, TR, BR, BL.",
    )


class ReceiptProcessResponse(BaseModel):
    """Response of ``POST /extract-receipt``."""

    receipt: ReceiptRead
    reconciliation: ReconciliationDetail
    boxes: list[OCRBox] = Field(
        default_factory=list,
        description="Detected text lines, in the coordinates of the stored image.",
    )
    image_width: Optional[int] = Field(default=None, ge=1, description="Width of the OCR input image.")
    image_height: Optional[int] = Field(default=None, ge=1, description="Height of the OCR input image.")
    ocr_confidence: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Mean OCR confidence over all detected text lines.",
    )
    processing_time_ms: float = Field(ge=0.0, description="End-to-end processing time.")
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal issues (e.g. low OCR confidence, missing fields).",
    )


class ReceiptDetailResponse(BaseModel):
    """Response of ``GET /expenses/{id}``: the stored receipt and why it is (not) verified."""

    receipt: ReceiptRead
    reconciliation: ReconciliationDetail


# ---------------------------------------------------------------------------
# Dashboard statistics
# ---------------------------------------------------------------------------
class CurrencyTotal(BaseModel):
    """Aggregated spending for one currency."""

    currency: Currency
    total_amount: Money
    tax_amount: Money
    receipt_count: int = Field(ge=0)


class MonthlyTotal(BaseModel):
    """Aggregated spending for one calendar month and currency."""

    month: str = Field(
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        description="Month in YYYY-MM format.",
        examples=["2026-11"],
    )
    currency: Currency
    total_amount: Money
    receipt_count: int = Field(ge=0)


class CategoryTotal(BaseModel):
    """Aggregated spending for one category and currency."""

    category: Category
    currency: Currency
    total_amount: Money
    receipt_count: int = Field(ge=0)


class MerchantInfo(BaseModel):
    """One merchant as the filter / management UI needs it."""

    merchant_key: str
    merchant_name: str = Field(description="A representative spelling.")
    receipt_count: int = Field(ge=0)
    category: Optional[Category] = Field(default=None, description="Learned category rule, if any.")


class MerchantMergeRequest(BaseModel):
    """``POST /merchants/merge``: treat ``from_key`` as another spelling of ``into_key``."""

    model_config = ConfigDict(extra="forbid")

    from_key: str = Field(min_length=1, max_length=_MERCHANT_MAX_LENGTH)
    into_key: str = Field(min_length=1, max_length=_MERCHANT_MAX_LENGTH)


class MerchantMergeResponse(BaseModel):
    """Result of a merchant merge."""

    moved: int = Field(ge=0, description="Receipts that now belong to ``into_key``.")


class MerchantTotal(BaseModel):
    """Aggregated spending for one merchant and currency."""

    merchant_key: Optional[str] = None
    merchant_name: str
    currency: Currency
    total_amount: Money
    receipt_count: int = Field(ge=0)


class StatsResponse(BaseModel):
    """Data backing the Streamlit dashboard."""

    total_receipts: int = Field(ge=0)
    verified_count: int = Field(ge=0)
    flagged_count: int = Field(ge=0)
    totals_by_currency: list[CurrencyTotal] = Field(default_factory=list)
    monthly_totals: list[MonthlyTotal] = Field(default_factory=list)
    category_totals: list[CategoryTotal] = Field(default_factory=list)
    top_merchants: list[MerchantTotal] = Field(
        default_factory=list, description="Ten biggest merchants per currency."
    )


# ---------------------------------------------------------------------------
# Operational
# ---------------------------------------------------------------------------
class HealthResponse(BaseModel):
    """Response of ``GET /health`` (used by the Docker healthcheck)."""

    status: Literal["ok", "degraded"]
    database: bool = Field(description="True when the database answers a trivial query.")
    version: str


class ErrorResponse(BaseModel):
    """Uniform error body documented in the OpenAPI schema."""

    detail: str
    code: Optional[str] = Field(default=None, description="Stable machine-readable error code.")
