"""GOCLL balance check — Sheet1 C2 SUMPRODUCT must equal 0.

The single hardest success criterion: the whole eFIS ``MonetaryAmount``
column (each batch's cash control line offsetting its detail lines) must
net to exactly zero.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from src.models.domain import CheckResult
from src.models.gocoll_assembly_models import GoCollAssemblyResult

logger = logging.getLogger(__name__)


def check_balance(
    assembly: GoCollAssemblyResult,
    tolerance: Decimal = Decimal("0.00"),
) -> CheckResult:
    """Verify the monetary total of all lines equals zero."""
    je = assembly.journal_entry
    total = je.monetary_total if je else Decimal("0")
    failures: list[str] = []

    passed = abs(total) <= tolerance
    if not passed:
        failures.append(f"MonetaryAmount total = {total} (expected 0, tolerance ±{tolerance})")
        logger.warning("GOCLL balance FAIL: total=%s", total)
    else:
        logger.info("GOCLL balance PASS: total=%s", total)

    return CheckResult(
        check_name="balance",
        status="PASS" if passed else "FAIL",
        details=f"MonetaryAmount total={total}, tolerance=±{tolerance}",
        failures=failures,
    )