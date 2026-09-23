"""Control totals check — verify batch cash offsets detail lines.

Every lockbox batch is one cash/control line plus N detail lines. The
cash line is ``-SUM(details)``, so each batch's ``batch_total`` must be
0. A non-zero batch means the cash line and its details disagree (a
classification splice or rounding defect).
"""

from __future__ import annotations

import logging
from decimal import Decimal

from src.models.domain import CheckResult
from src.models.gocoll_assembly_models import GoCollAssemblyResult

logger = logging.getLogger(__name__)


def check_control_totals(
    assembly: GoCollAssemblyResult,
    tolerance: Decimal = Decimal("0.00"),
) -> CheckResult:
    """Verify every batch's cash line offsets its detail lines."""
    je = assembly.journal_entry
    failures: list[str] = []

    if je is not None:
        for be in je.batch_entries:
            if be.cash_line is None:
                failures.append(f"Batch {be.batch_number}: missing cash/control line")
                continue
            total = be.batch_total
            if abs(total) > tolerance:
                failures.append(
                    f"Batch {be.batch_number}: net = {total} "
                    f"(cash={be.cash_line.monetary_amount}, details={be.detail_total})"
                )

    passed = len(failures) == 0
    if passed:
        logger.info("GOCOLL control totals PASS: %d batch(es) net to 0", len(je.batch_entries) if je else 0)
    else:
        for f in failures:
            logger.warning("GOCOLL control totals FAIL: %s", f)

    return CheckResult(
        check_name="control_totals",
        status="PASS" if passed else "FAIL",
        details=f"{len(je.batch_entries) if je else 0} batch(es) checked, tolerance={tolerance}",
        failures=failures,
    )