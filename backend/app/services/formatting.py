"""Number formatting for messages shown to the user (Turkish style)."""

from decimal import Decimal

__all__ = ["format_tr"]


def format_tr(value: Decimal) -> str:
    """``Decimal('1250.5')`` -> ``'1.250,50'``."""
    return f"{value:,.2f}".replace(",", "\0").replace(".", ",").replace("\0", ".")
