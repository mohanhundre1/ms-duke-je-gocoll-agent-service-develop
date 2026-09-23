"""Shared numeric and string helpers for the GOCOLL agent service."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any


def to_float(val: Any) -> float:
    """Convert Decimal / numeric / str -> float; returns 0.0 for None/unparseable."""
    if val is None:
        return 0.0
    if isinstance(val, Decimal):
        return float(val)
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def fmt_currency(value: Any, formatting: dict[str, Any] | None = None) -> str:
    """Format a numeric value as a currency string using SOP display conventions."""
    if value is None:
        return "-"
    formatting = formatting or {}
    decimals = int(formatting.get("decimal_places", 2))
    quantum = Decimal(1).scaleb(-decimals)
    try:
        d = Decimal(str(to_float(value))).quantize(quantum, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return "-"
    raw = f"{abs(d):,.{decimals}f}"
    symbol = str(formatting.get("currency_symbol", "$"))
    if d < 0 and formatting.get("negative_style", "parentheses") == "parentheses":
        return f"({symbol}{raw})"
    if d < 0:
        return f"-{symbol}{raw}"
    return f"{symbol}{raw}"


def strip_dot_zero(text: str) -> str:
    """Drop a trailing '.0' left by openpyxl/pandas when reading numeric cells as strings."""
    return text[:-2] if isinstance(text, str) and text.endswith(".0") else text


#: eFIS / Chart of Accounts store an Account as zero-padded text of this width.
ACCOUNT_WIDTH = 7


def normalize_account(value: Any) -> str:
    """Return an Account code in the eFIS form: 7-char zero-padded when numeric.

    The Validation Tab and Chart of Accounts hold accounts as 7-character
    zero-padded text (`0107000`). A GO form is often transcribed without the
    leading zero, and eFIS rejects that shorter form, so pad a numeric code and
    pass anything else (alphanumeric, already-wide, blank) through untouched.
    """
    text = strip_dot_zero(str(value or "")).strip()
    if text.isdigit() and len(text) < ACCOUNT_WIDTH:
        return text.zfill(ACCOUNT_WIDTH)
    return text"""Placeholder for utils.py."""
