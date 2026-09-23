"""Schema validation for AI extraction outputs.

Applies strict structural checks to LLM/Vision JSON payloads before
pipeline consumption. This module intentionally validates only AI outputs;
regex/local extractors can keep their existing broader payload shapes.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

logger = logging.getLogger(__name__)


class _AIModel(BaseModel):
    model_config = ConfigDict(extra="allow")


def _to_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        normalized = value.strip().replace(",", "")
        if normalized == "":
            raise ValueError(f"{field_name} must not be empty")
        try:
            return Decimal(normalized)
        except InvalidOperation as exc:
            raise ValueError(f"{field_name} must be numeric") from exc
    raise ValueError(f"{field_name} must be numeric")


def _validate_confidence(confidence: Any) -> dict[str, float]:
    if confidence is None:
        return {}
    if not isinstance(confidence, dict):
        raise ValueError("confidence must be an object")

    # Confidence is diagnostic: it steers re-reads and analyst routing but never
    # changes an extracted amount. A malformed score is therefore dropped rather
    # than failing the page - losing a whole grid because one advisory number came
    # back as "high" instead of 0.9 would be a bad trade.
    validated: dict[str, float] = {}
    for key, value in confidence.items():
        if not isinstance(key, str):
            continue
        try:
            score = float(value)
        except (TypeError, ValueError):
            logger.debug("Dropping non-numeric confidence[%s]=%r", key, value)
            continue
        if not 0.0 <= score <= 1.0:
            logger.debug("Dropping out-of-range confidence[%s]=%r", key, score)
            continue
        validated[key] = score
    return validated


CODING_DIMENSIONS = (
    "business_unit", "account", "resource_type", "operating_unit",
    "resp_center", "project", "activity_id", "process", "location",
    "product", "affiliate", "alloc_pool", "line_descr",
)

# A ruled-but-empty grid cell comes back as one of these. "0" is included because
# the model fills empty numeric cells with a bare zero on some scans.
_BLANK_CELL_TOKENS = frozenset({"", "0", "0.00", "none", "n/a", "-", "--"})


def _is_zero_amount(value: Any) -> bool:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return True
    try:
        return Decimal(str(value).strip().replace(",", "").replace("$", "")) == 0
    except (InvalidOperation, ValueError):
        return False


def is_blank_grid_row(line: Any) -> bool:
    """True only for a ruled-but-empty grid row the model transcribed as zeros.

    Fires when the amount is zero AND every coding dimension is blank or a bare
    "0". A zero-amount row carrying genuine coding is a real distribution whose
    amount failed to read - it must survive and be flagged, never dropped. Two
    scanned batches produce each kind, so the distinction is not hypothetical.
    """
    if not isinstance(line, dict):
        return False
    if not _is_zero_amount(line.get("amount")):
        return False
    return all(
        str(line.get(dim) or "").strip().lower() in _BLANK_CELL_TOKENS
        for dim in CODING_DIMENSIONS
    )


def is_unpriced_coded_row(line: Any) -> bool:
    """A real code block whose amount did not read: keep it, but it needs a re-read.

    Booking this as zero yields a JE that balances while silently under-distributing.
    """
    if not isinstance(line, dict):
        return False
    return _is_zero_amount(line.get("amount")) and not is_blank_grid_row(line)


class GoColFormLine(_AIModel):
    """One accounting distribution line transcribed from a GO-Collection-Form."""
    amount: Decimal

    @model_validator(mode="before")
    @classmethod
    def _coerce_fields(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            raise ValueError("go-form lines must be objects")
        out = dict(values)
        # Coding dimensions are optional strings; normalize None -> "".
        for key in CODING_DIMENSIONS:
            val = values.get(key, "")
            out[key] = "" if val is None else str(val).strip()
        # A coded line may carry a valid code block but a missing/unreadable amount;
        # keep the line (amount defaults to 0) instead of failing the whole page.
        amt = values.get("amount")
        if amt is None or (isinstance(amt, str) and amt.strip() == ""):
            out["amount"] = Decimal("0")
        else:
            out["amount"] = _to_decimal(amt, "amount")
        return out


class GoColFormPayload(_AIModel):
    """One unified page read from a GOCOLL lockbox batch.

    A page either carries a GL coding grid (`lines` populated) or is a
    check / Wells Fargo / deposit page (`lines` empty). Either way it may
    report the check-identifying fields used to group pages into checks.
    """

    lines: list[GoColFormLine] = []
    check_number: str = ""
    batch_number: str = ""
    sequence_number: str = ""
    transaction_type: str = ""
    transaction_total: Decimal = Decimal("0")
    form_check_amounts: list[Decimal] = []
    form_check_amount_raw: str = ""
    form_total: Decimal = Decimal("0")
    form_total_raw: str = ""
    handwritten_fields: list[str] = []
    check_amount: Decimal = Decimal("0")
    confidence: dict[str, float] | None = None
    # Coded rows whose amount did not read. Non-zero means the page's distribution is
    # incomplete even if its total happens to tie.
    unpriced_line_count: int = 0

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            raise ValueError("go-form payload must be an object")
        out = dict(values)
        raw_lines = out.get("lines")
        if raw_lines is None:
            raw_lines = []
        if isinstance(raw_lines, (list, tuple)):
            kept = [ln for ln in raw_lines if not is_blank_grid_row(ln)]
            if len(kept) != len(raw_lines):
                logger.debug(
                    "Dropped %d blank grid row(s) transcribed as zeros",
                    len(raw_lines) - len(kept),
                )
            unpriced = sum(1 for ln in kept if is_unpriced_coded_row(ln))
            if unpriced:
                logger.warning(
                    "%d coded row(s) have no readable amount; page needs a re-read "
                    "before its distribution can be trusted", unpriced,
                )
            out["unpriced_line_count"] = unpriced
            raw_lines = kept
        out["lines"] = raw_lines
        for _id in (
            "check_number", "batch_number", "sequence_number",
            "transaction_type", "form_check_amount_raw", "form_total_raw",
        ):
            val = values.get(_id, "")
            out[_id] = "" if val is None else str(val).strip()
        # Diagnostic only: names the fields the model saw as handwriting, so a
        # re-read can be aimed at the right box instead of the whole page.
        raw_hw = values.get("handwritten_fields")
        out["handwritten_fields"] = (
            [str(f).strip() for f in raw_hw if f is not None and str(f).strip()]
            if isinstance(raw_hw, (list, tuple)) else []
        )
        # Amount-like fields may be absent on a page that isn't a check/coding
        # page; default them to 0 instead of failing the whole read.
        for _amt in ("check_amount", "form_total", "transaction_total"):
            raw = values.get(_amt)
            if raw is None or (isinstance(raw, str) and raw.strip() == ""):
                out[_amt] = Decimal("0")
            else:
                out[_amt] = _to_decimal(raw, _amt)
        # Section D "CHECK AMOUNT" is a free-text box: one preparer writes the summed
        # amount ("39,531.99"), another lists each check ("$467.52 and $65.74"). The
        # model transcribes every amount it finds and never adds them, so the caller
        # can use the element count as a direct multi-check signal. A single
        # unparseable entry is dropped rather than failing the whole page read.
        raw_list = values.get("form_check_amounts")
        parsed: list[Decimal] = []
        if isinstance(raw_list, (list, tuple)):
            for item in raw_list:
                if item is None or (isinstance(item, str) and not item.strip()):
                    continue
                try:
                    parsed.append(_to_decimal(item, "form_check_amounts[]"))
                except ValueError:
                    continue
        elif raw_list is not None and str(raw_list).strip():
            try:
                parsed.append(_to_decimal(raw_list, "form_check_amounts"))
            except ValueError:
                pass
        out["form_check_amounts"] = parsed
        # legacy alias, still consumed throughout the coordinator. The bank's printed
        # "Check Amount" was extracted separately for a while; it equaled
        # ``transaction_total`` on all 80 pages that carried both and never appeared
        # without it, so it was dropped. Re-introduce it only if a lockbox starts
        # placing more than one check in a transaction, which the WF report would
        # show as a transaction with several check rows.
        if not out["check_amount"]:
            out["check_amount"] = out["transaction_total"]
        out["confidence"] = _validate_confidence(values.get("confidence"))
        return out


_SCHEMAS: dict[str, type[_AIModel]] = {
    "gocoll_go_form": GoColFormPayload,
}


def validate_ai_extraction_output(pdf_type: str, data: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate and normalize AI extraction output for a PDF type.

    Returns:
        (normalized_data, None) when valid.
        (None, error_message) when invalid.
    """
    if not isinstance(data, dict):
        return None, "payload must be a JSON object"

    schema = _SCHEMAS.get(pdf_type)
    if schema is None:
        return data, None

    try:
        parsed = schema.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(err.get("msg", "invalid payload") for err in exc.errors()[:3])
        return None, f"schema validation failed for {pdf_type}: {details}"

    return parsed.model_dump(mode="python"), None