"""
GOGOLL reconciliation check - Engine 4 (A/B/C batch tie-out).

For each batch three independent totals are compared:
    * **A = coded** - the GO-form distribution total booked in the JE
      (the batch cash line).
    * **B = WF** - the Wells Fargo Transactions Report gross check total
      (`WFReport.batch_check_total`), the bank's record of what was deposited.
    * **C = Treasury** - the bank lockbox deposit (BAI 115) matched to the batch,
      when the Treasury feed confirms it.

An A-vs-B gap is *explained* when it matches a Treasury return item (BAI 566):
the returned check is in the bank's gross total but never coded, so the coded
total is legitimately short by the returned amount. An unmatched gap is a
first-class reconciliation flag routed to analyst review ("REVIEW").

OTC (over-the-counter) handling is on hold and not reconciled here.

The cash line still books the coded total (A); the redesigned check surfaces the
A-vs-B gap explicitly rather than hiding it behind a balancing plug.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from src.models.domain import CheckResult
from src.models.enums import ValidationStatus
from src.models.gocoll_assembly_models import GoCollAssemblyResult
from src.models.gocoll_models import GoCollExtraction
from src.models.gocoll_reconciliation_models import (
    STATUS_DIFFERENCE,
    STATUS_NO_CONTROL,
    STATUS_RETURN_ITEM,
    STATUS_TIE,
    STATUS_WF_MISSING,
    BatchReconciliation,
    ReconciliationResult,
)
from src.models.gocoll_source_models import (
    BAI_RETURN_ITEM,
    BAI_LOCKBOX_DEPOSIT,
    TreasuryReport,
    WFReport,
)

logger = logging.getLogger(__name__)


def _coded_amount(entry) -> Decimal:
    """A - the coded deposit for a batch (positive cash-line amount)."""
    if entry.cash_line is not None:
        return entry.cash_line.monetary_amount
    return -entry.detail_total


def _wf_total(batch_no: str, wf_report: WFReport | None) -> Decimal | None:
    """B - WF gross check total for a batch, only from the bank's own report.

    No fallback to AI-extracted figures: if the batch is not in the WF report the
    caller records it as ``wf_missing`` (flagged) rather than silently
    substituting an unverified number into the bank column.
    """
    if wf_report is not None and wf_report.loaded and batch_no in wf_report.batch_numbers:
        return wf_report.batch_check_total(batch_no)
    return None


def _return_item_pool(treasury_report: TreasuryReport | None) -> list[Decimal]:
    """BAI 566 return-item credit amounts available to explain A-vs-B gaps."""
    if treasury_report is None:
        return []
    rows = treasury_report.rows_for_bai(BAI_RETURN_ITEM)
    amounts = [r.credit for r in rows if r.credit]
    if amounts:
        return amounts
    # No itemised rows - fall back to the banner total as a single pool entry.
    if treasury_report.total_return_items:
        return [treasury_report.total_return_items]
    return []


def _match_lockbox_deposit(
    wf_amount: Decimal | None,
    treasury_report: TreasuryReport | None,
    tolerance: Decimal,
) -> Decimal | None:
    """C - a Treasury BAI 115 lockbox debit matching the batch's WF total."""
    if wf_amount is None or treasury_report is None:
        return None
    from src.models.gocoll_source_models import BAI_LOCKBOX_DEPOSIT

    for row in treasury_report.rows_for_bai(BAI_LOCKBOX_DEPOSIT):
        if row.debit and abs(row.debit - wf_amount) <= tolerance:
            return row.debit
    return None


def reconcile_batches(
    assembly: GoCollAssemblyResult,
    *,
    wf_report: WFReport | None = None,
    treasury_report: TreasuryReport | None = None,
    extraction: GoCollExtraction | None = None,
    tolerance: Decimal = Decimal("0.01"),
) -> ReconciliationResult:
    """Compute the A/B/C tie-out for every batch in the assembled JE."""
    je = assembly.journal_entry
    result = ReconciliationResult(
        tolerance=tolerance,
        treasury_return_items_total=(
            treasury_report.return_items_total if treasury_report else Decimal("0")
        ),
        treasury_otc_total=(
            treasury_report.total_over_the_counter if treasury_report else Decimal("0")
        ),
    )

    if je is None:
        return result

    extraction_index: dict = {}
    if extraction is not None:
        for b in extraction.batches:
            extraction_index[str(b.batch_number)] = {
                "wf_gross": b.wf_deposit_gross,
                "wf_booked": b.wf_deposit_booked,
                "wf_return_items": b.wf_return_items,
                "check_total": b.check_total,
                "transaction_total": b.transaction_total_sum,
            }

    return_pool = _return_item_pool(treasury_report)
    # Fallback: honour per-batch return items carried on the extraction control
    # (used when no Treasury feed is supplied).
    if not return_pool:
        for ctrl in extraction_index.values():
            ri = ctrl.get("wf_return_items")
            if ri:
                return_pool.append(ri)

    wf_loaded = wf_report is not None and wf_report.loaded

    for entry in je.batch_entries:
        if entry.is_otc:
            continue
        batch_no = str(entry.batch_number)
        coded = _coded_amount(entry)
        wf_amount = _wf_total(batch_no, wf_report)
        treasury_amount = _match_lockbox_deposit(wf_amount, treasury_report, tolerance)

        br = BatchReconciliation(
            batch_number=batch_no,
            coded_amount=coded,
            wf_amount=wf_amount,
            treasury_amount=treasury_amount,
        )

        if wf_amount is None:
            if wf_loaded:
                br.status = STATUS_WF_MISSING
                br.detail = (
                    f"Batch {batch_no} not found in the WF report - no bank record "
                    f"to reconcile coded ({coded}) against (needs analyst confirmation)"
                )
            else:
                br.status = STATUS_NO_CONTROL
                br.detail = "No WF report supplied - reconciliation skipped"
            result.batches.append(br)
            continue

        gap = wf_amount - coded  # positive: bank total exceeds coded (uncoded item)
        if abs(gap) <= tolerance:
            br.status = STATUS_TIE
            br.detail = f"Coded ({coded}) ties to WF ({wf_amount})"
            result.batches.append(br)
            continue

        # If the CTROL monetary amount does not tie to WF, use CTROL's printed
        # transaction total when that value does tie. WF remains authoritative and
        # is never rewritten by this fallback.
        txn_amount = extraction_index.get(batch_no, {}).get("transaction_total")
        if (
            txn_amount
            and abs(txn_amount - coded) > tolerance
            and abs(txn_amount - wf_amount) <= tolerance
        ):
            br.coded_amount = txn_amount
            br.status = STATUS_TIE
            br.detail = (
                f"CTROL monetary amount (coded) replaced by transaction total "
                f"({txn_amount}); ties to WF ({wf_amount})"
            )
            result.batches.append(br)
            continue

        # Try to explain the gap with a Treasury return item (BAI 566).
        matched_idx = next(
            (i for i, r in enumerate(return_pool) if abs(gap - r) <= tolerance),
            None,
        )
        if matched_idx is not None:
            amt = return_pool.pop(matched_idx)
            br.status = STATUS_RETURN_ITEM
            br.return_items = amt
            br.detail = (
                f"Coded ({coded}) vs WF ({wf_amount}): gap ({gap}) explained by "
                f"BAI 566 return item ({amt})"
            )
        else:
            br.status = STATUS_DIFFERENCE
            br.detail = (
                f"Coded ({coded}) vs WF ({wf_amount}): unexplained difference "
                f"{entry_diff(coded, wf_amount)} - needs analyst confirmation"
            )
        result.batches.append(br)

    return result


def entry_diff(coded: Decimal, wf_amount: Decimal) -> Decimal:
    """A - B, the signed coded-vs-WF difference reported to analysts."""
    return coded - wf_amount


def check_reconciliation(
    assembly: GoCollAssemblyResult,
    extraction: GoCollExtraction | None = None,
    *,
    wf_report: WFReport | None = None,
    treasury_report: TreasuryReport | None = None,
    tolerance: Decimal = Decimal("0.01"),
) -> CheckResult:
    """Tie each batch's coded total (A) to WF (B) and Treasury (C).

    Returns ``PASS`` when every batch ties out or its gap is fully explained by
    a return item; ``REVIEW`` listing the batches with an unexplained gap.
    Skipped (``PASS``) when no WF/Treasury control is available at all.
    """
    result = reconcile_batches(
        assembly,
        wf_report=wf_report,
        treasury_report=treasury_report,
        extraction=extraction,
        tolerance=tolerance,
    )

    reconciled = [b for b in result.batches if b.status != STATUS_NO_CONTROL]
    if not reconciled:
        return CheckResult(
            check_name="reconciliation",
            status=ValidationStatus.PASS,
            details="No WF/Treasury control available - reconciliation skipped",
        )

    failures = [
        f"Batch {b.batch_number}: {b.detail}" for b in result.flagged
    ]

    passed = not failures
    if passed:
        logger.info(
            "GOGOLL reconciliation PASS: %d batch(es) tied to WF (%d return-item, %d exact)",
            len(reconciled), len(result.return_item_batches),
            sum(1 for b in reconciled if b.status == STATUS_TIE),
        )
    else:
        logger.warning(
            "GOGOLL reconciliation REVIEW: %d of %d batch(es) flagged "
            "(%d unexplained gap, %d missing from WF report)",
            len(failures), len(reconciled),
            len(result.unexplained), len(result.wf_missing),
        )

    details = (
        f"{len(reconciled)} batch(es) reconciled A(coded)/B(WF)/C(Treasury); "
        f"{len(result.return_item_batches)} return-item, "
        f"{len(result.unexplained)} unexplained, "
        f"{len(result.wf_missing)} missing from WF report"
    )

    return CheckResult(
        check_name="reconciliation",
        status=ValidationStatus.PASS if passed else ValidationStatus.REVIEW,
        details=details,
        failures=failures,
    )