"""JSON serialization helpers for the GOCOLL A2A response."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any


def decimal_default(obj: Any) -> Any:
    """json.dump default handler for Decimal / date / datetime."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")"""Placeholder for serializers.py."""
