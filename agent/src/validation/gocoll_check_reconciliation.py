"""Check-level reconciliation (pre-assembly).

For each batch, ties GO-form code blocks to WF checks:

Step 1 - Shared-form detection:
  A GO form is shared across checks when it has more than one code block AND
  sum(section D CHECK AMOUNT) > transaction_total (both PDF-extracted fields).
  Each code block's coded total is matched individually to a WF check amount.

Step 2 - Digit correction (≥ 50% positional digit-match gate):
  When no direct match is found, a digit correction is attempted. Gated by
  requiring at least 50% of digit positions to agree between the coded amount
  and the WF candidate. Corrections are applied in place so the corrected
  figures flow into the assembled JE.

Step 3b - Check-level reconciliation flags:
  Coded amounts with no WF match -> no_wf_match.
  Unclaimed WF pool entries after all coded forms are processed -> missing_coding.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from src.validation.gocoll_check_matcher import find_digit_correction

logger = logging.getLogger(__name__)

_TOL = Decimal("0.01")
_MIN_DIGIT_MATCH = 0.5


def _digit_str(amount: Decimal) -> str:
    return "".join(c for c in str(abs(amount)) if c.isdigit())


def _positional_match_fraction(a: Decimal, b: Decimal) -> float:
    """Fraction of digit positions that agree between two amounts (same length only)."""
    da, db = _digit_str(a), _digit_str(b)
    if len(da) != len(db) or not da:
        return 0.0
    return sum(ca == cb for ca, cb in zip(da, db)) / len(da)


def _money(value: Decimal | str) -> float | str:
    if value == "" or value is None:
        return ""
    return float(value)


def _row(batch: str, check: str, coded, wf, status: str, detail: str) -> dict[str, Any]:
    diff: Decimal | str = ""
    if isinstance(coded, Decimal) and isinstance(wf, Decimal):
        diff = coded - wf
    return {
        "batch": batch,
        "check": check,
        "coded": _money(coded),
        "wf": _money(wf),
        "diff": _money(diff),
        "status": status,
        "detail": detail,
    }


def reconcile_and_correct(
    extraction: Any,
    wf_report: Any,
    *,
    tolerance: Decimal = _TOL,
) -> list[dict[str, Any]]:
    """Reconcile coded GO-form code blocks to WF checks; correct misreads in place.

    Populates ``extraction.check_recon_rows`` and returns it. When no WF report is
    loaded the rows are empty (there is no bank record to tie to).
    """
    rows: list[dict[str, Any]] = []
    if extraction is None:
        return rows
    if wf_report is not None and getattr(wf_report, "loaded", False):
        for batch in getattr(extraction, "batches", []) or []:
            rows.extend(_reconcile_batch_checks(batch, wf_report, tolerance))
    extraction.check_recon_rows = rows
    return rows


def _reconcile_batch_checks(batch: Any, wf_report: Any, tol: Decimal) -> list[dict[str, Any]]:
    batch_no = getattr(batch, "batch_number", "")
    # Mutable WF pool: [check_number, amount, consumed].
    wf_pool: list[list] = [
        [str(getattr(t, "check_number", "") or ""), abs(t.check_amount), False]
        for t in wf_report.transactions_for(batch_no)
        if t.check_amount
    ]

    def take(amount: Decimal) -> list | None:
        for entry in wf_pool:
            if not entry[2] and abs(entry[1] - amount) <= tol:
                entry[2] = True
                return entry
        return None

    def take_with_correction(coded: Decimal) -> tuple[list | None, Decimal | None]:
        """Direct match first; then digit correction gated at ≥50% positional digit overlap."""
        hit = take(coded)
        if hit is not None:
            return hit, None
        available = [(e[0], e[1]) for e in wf_pool if not e[2]]
        gated = [
            (num, amt) for num, amt in available
            if _positional_match_fraction(coded, amt) >= _MIN_DIGIT_MATCH
        ]
        if not gated:
            return None, None
        correction = find_digit_correction(coded, gated, tol)
        if correction is None:
            return None, None
        corrected_amt = correction
        entry = take(corrected_amt)
        return (entry, corrected_amt) if entry else (None, None)

    rows: list[dict[str, Any]] = []
    checks = list(getattr(batch, "checks", []) or [])
    coded_checks = [c for c in checks if getattr(c, "lines", None)]
    uncoded_checks = [c for c in checks if not getattr(c, "lines", None)]

    for idx, check in enumerate(coded_checks):
        label = str(getattr(check, "sequence_number", "") or "").strip() or str(idx + 1)
        extracted_check = str(getattr(check, "check_number", "") or "").strip()
        lines = list(check.lines)
        n_code_blocks = len(lines)
        declared_total = getattr(check, "declared_total", Decimal("0")) or Decimal("0")
        transaction_total = getattr(check, "transaction_total", Decimal("0")) or Decimal("0")

        # Shared-form: multiple code blocks AND section D total exceeds the
        # transaction total on this check page -> form spans multiple checks.
        # Fallback: when PDF fields are absent but the sum finds no WF match,
        # still attempt per-block matching so multi-check forms are not flagged
        # as a single aggregated no_wf_match.
        is_shared_by_fields = (
            n_code_blocks > 1
            and transaction_total > tol
            and declared_total > transaction_total + tol
        )

        if is_shared_by_fields:
            rows.extend(_match_blocks(
                batch_no, label, lines, n_code_blocks, take_with_correction, tol,
            ))
        else:
            coded_total = sum(abs(ln.amount) for ln in lines) if lines else Decimal("0")
            hit, corrected_amt = take_with_correction(coded_total)
            if hit is not None:
                if corrected_amt is not None:
                    if len(lines) == 1:
                        ln = lines[0]
                        ln.amount = corrected_amt if ln.amount >= 0 else -corrected_amt
                    logger.info(
                        "GOCOLL batch %s check %s: corrected %s -> %s",
                        batch_no, label, coded_total, corrected_amt,
                    )
                    rows.append(_row(
                        batch_no, hit[0] or extracted_check, corrected_amt, hit[1], "tie",
                        f"Coded amount corrected {coded_total} -> {corrected_amt} to tie WF",
                    ))
                else:
                    rows.append(_row(
                        batch_no, hit[0] or extracted_check, coded_total, hit[1], "tie",
                        "Coded check total ties to WF check amount",
                    ))
            elif n_code_blocks > 1:
                # Try per-block fallback for forms without section D fields, but only
                # accept if every block matches - prevents single-check multi-distribution
                # forms from stealing WF entries via false splits.
                pool_state = [e[2] for e in wf_pool]
                block_rows = _match_blocks(
                    batch_no, label, lines, n_code_blocks, take_with_correction, tol,
                )
                if any(r["status"] == "no_wf_match" for r in block_rows):
                    for i, entry in enumerate(wf_pool):
                        entry[2] = pool_state[i]
                    rows.append(_row(
                        batch_no, extracted_check, coded_total, "", "no_wf_match",
                        f"Coded total {coded_total} matches no WF check amount in batch "
                        f"{batch_no} - verify the coded amount against the bank",
                    ))
                else:
                    rows.extend(block_rows)
            else:
                rows.append(_row(
                    batch_no, extracted_check, coded_total, "", "no_wf_match",
                    f"Coded total {coded_total} matches no WF check amount in batch "
                    f"{batch_no} - verify the coded amount against the bank",
                ))

    # Consume uncoded checks last so shared-form splits claim their WF entries first.
    # When a WF entry is found (not already consumed), inject a placeholder transaction
    # so the check appears in Sheet 1 and Extracted Data flagged for analyst review.
    for check in uncoded_checks:
        amt = abs(getattr(check, "check_amount", Decimal("0")) or Decimal("0"))
        hit = take(amt)
        if hit is not None and amt > tol:
            _inject_review_transaction(check, batch_no, amt)
            rows.append(_row(
                batch_no,
                hit[0] or str(getattr(check, "check_number", "") or ""),
                amt, hit[1], "tie",
                f"Check amount {amt} ties to WF - no GO form found, "
                f"analyst must supply accounting dimensions",
            ))

    # Leftover WF entries -> flag for analyst review.
    for entry in wf_pool:
        if not entry[2]:
            rows.append(_row(
                batch_no, entry[0], "", entry[1], "missing_coding",
                f"WF check amount {entry[1]} in batch {batch_no} has no matching "
                f"coded check - a check may be missing or misread",
            ))

    return rows


def _match_blocks(
    batch_no: str,
    label: str,
    lines: list,
    n_code_blocks: int,
    take_with_correction,
    tol: Decimal,
) -> list[dict[str, Any]]:
    """Match each code block individually to a WF check entry."""
    rows: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        coded_amt = abs(line.amount)
        block_label = f"{label}.{i + 1}"
        hit, corrected_amt = take_with_correction(coded_amt)
        if hit is not None:
            if corrected_amt is not None:
                line.amount = corrected_amt if line.amount >= 0 else -corrected_amt
                logger.info(
                    "GOCOLL batch %s check %s block %d: corrected %s -> %s",
                    batch_no, label, i + 1, coded_amt, corrected_amt,
                )
                rows.append(_row(
                    batch_no, hit[0], corrected_amt, hit[1], "tie",
                    f"Shared form block {i + 1}/{n_code_blocks}: "
                    f"corrected {coded_amt} -> {corrected_amt} to tie WF",
                ))
            else:
                rows.append(_row(
                    batch_no, hit[0], coded_amt, hit[1], "tie",
                    f"Shared form block {i + 1}/{n_code_blocks}: "
                    f"coded amount ties to WF check",
                ))
        else:
            rows.append(_row(
                batch_no, str(getattr(line, "check_number", "") or ""),
                coded_amt, "", "no_wf_match",
                f"Shared form block {i + 1}/{n_code_blocks}: coded amount "
                f"{coded_amt} matches no WF check in batch {batch_no}",
            ))
    return rows


def _inject_review_transaction(check: Any, batch_no: str, amt: Decimal) -> None:
    """Add a needs_review placeholder so the check posts to Sheet1 with blank coding."""
    from src.models.gocoll_models import GoCollCodeBlock, GoCollTransaction

    txn = GoCollTransaction(
        batch_number=batch_no,
        sequence=0,
        check_number=getattr(check, "check_number", "") or "",
        check_amount=amt,
        amount=-amt,  # negative detail line; cash line = -SUM(details) balances
        needs_review=True,
        review_reason="No GO-form coding found - analyst must supply accounting dimensions",
        code_block=GoCollCodeBlock(),
    )
    try:
        if not check.lines:
            check.lines = []
        check.lines.append(txn)
    except AttributeError:
        pass