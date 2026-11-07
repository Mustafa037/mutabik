"""SQLAlchemy ORM models.

An :class:`Expense` row is the persisted result of one processed receipt. The
API layer exposes it through the ``Receipt*`` schemas in
``app.schemas.receipt``.
"""

import datetime as dt
import enum
import warnings
from decimal import Decimal
from typing import Optional

from sqlalchemy import Date, DateTime, Index, Integer, Numeric, String, Text, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.exc import SAWarning
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.schemas.receipt import Category, Currency, ReconciliationStatus

# SQLite has no native DECIMAL type, so SQLAlchemy stores NUMERIC as float and
# warns about it. Amounts are limited to 2 decimals / 12 digits, which a double
# represents exactly after rounding, and the result processor converts them back
# to ``Decimal`` using the column scale. On PostgreSQL the type is native and
# the warning never fires.
warnings.filterwarnings(
    "ignore",
    message=r".*does \*not\* support Decimal objects natively.*",
    category=SAWarning,
)


def _utcnow() -> dt.datetime:
    """Return the current timezone-aware UTC datetime."""
    return dt.datetime.now(dt.timezone.utc)


def _enum_values(enum_class: type[enum.Enum]) -> list[str]:
    """Return the *values* of an enum so the DB stores them instead of names."""
    return [member.value for member in enum_class]


class Expense(Base):
    """A processed receipt / expense record.

    Attributes:
        id: Auto-incrementing primary key.
        merchant_name: Business name extracted from the receipt.
        date: Transaction date.
        subtotal_amount: Pre-tax amount (matrah); needed to audit reconciliation.
        tax_amount: Total tax (KDV / VAT / sales tax).
        total_amount: Grand total paid.
        currency: ISO-4217 code (``TRY``, ``USD``, ``EUR``, ``GBP``).
        category: Spending category (``None`` on rows created before categories existed).
        merchant_key: Normalised merchant identity (see ``services.merchant``); groups OCR spelling variants.
        reconciliation_status: Result of ``subtotal + tax == total``.
        reconciliation_method: How the subtotal was obtained (``printed``, ``derived_from_rate``,
            ``implied_rate``, ``manual``); ``None`` on older rows.
        raw_text: Full OCR text, kept for auditing and re-parsing.
        image_path: Where the (possibly pre-processed) image is stored.
        created_at: Row creation time (UTC).
    """

    __tablename__ = "expenses"
    __table_args__ = (
        # Dashboard queries group by currency and filter / sort by date.
        Index("ix_expenses_currency_date", "currency", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    merchant_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    date: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True, index=True)
    subtotal_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    tax_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    total_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[Currency] = mapped_column(
        SQLEnum(
            Currency,
            name="currency",
            native_enum=False,
            length=3,
            values_callable=_enum_values,
            validate_strings=True,
        ),
        nullable=False,
        default=Currency.TRY,
        server_default=Currency.TRY.value,
    )
    merchant_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    category: Mapped[Optional[Category]] = mapped_column(
        SQLEnum(
            Category,
            name="category",
            native_enum=False,
            length=32,
            values_callable=_enum_values,
            validate_strings=True,
        ),
        nullable=True,
        index=True,
    )
    reconciliation_status: Mapped[ReconciliationStatus] = mapped_column(
        SQLEnum(
            ReconciliationStatus,
            name="reconciliation_status",
            native_enum=False,
            length=20,
            values_callable=_enum_values,
            validate_strings=True,
        ),
        nullable=False,
        default=ReconciliationStatus.FLAGGED_FOR_REVIEW,
        server_default=ReconciliationStatus.FLAGGED_FOR_REVIEW.value,
        index=True,
    )
    reconciliation_method: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        """Compact representation for logs and debugging (omits ``raw_text``)."""
        return (
            f"Expense(id={self.id!r}, merchant_name={self.merchant_name!r}, "
            f"date={self.date!r}, total_amount={self.total_amount!r}, "
            f"currency={self.currency!r}, status={self.reconciliation_status!r})"
        )


class MerchantRule(Base):
    """What the user taught the system about one merchant (learned from corrections).

    Attributes:
        merchant_key: Normalised merchant identity (primary key).
        category: Category to give future receipts of this merchant.
        updated_at: When the rule was last set (UTC).
    """

    __tablename__ = "merchant_rules"

    merchant_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    category: Mapped[Category] = mapped_column(
        SQLEnum(
            Category,
            name="category",
            native_enum=False,
            length=32,
            values_callable=_enum_values,
            validate_strings=True,
        ),
        nullable=False,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
