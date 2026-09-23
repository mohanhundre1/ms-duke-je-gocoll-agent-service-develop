"""GOCOLL line count check - optional Sheet1 A2 (COUNTA) guard.

GOCOLL line counts vary week to week, so this check is disabled by
default in ``gocoll_v1``. When an ``expected`` count is supplied (via the
rule pack threshold) the actual ``line_count`` must match it exactly.
"""

from __future__ import annotations

import logging

from src.models.domain import CheckResult
from src.models.gocoll_assembly_models import GoCollAssemblyResult

logger = logging.getLogger(__name__)


def check_line_count(
    assembly: GoCollAssemblyResult,
    expected: int | None = None,
) -> CheckResult:
    """Verify total line count matches the expected count, if configured."""
    je = assembly.journal_entry
    actual = je.line_count if je else 0

    if expected is None:
        return CheckResult(
            check_name="line_count",
            status="PASS",
            details="Line count check skipped - no expected count configured",
        )

    failures: list[str] = []
    if actual != expected:
        failures.append(f"Line count: expected={expected}, actual={actual}")

    passed = len(failures) == 0
    if passed:
        logger.info("GOCOLL line count PASS: %d", actual)
    else:
        logger.warning("GOCOLL line count FAIL: expected=%d, actual=%d", expected, actual)

    return CheckResult(
        check_name="line_count",
        status="PASS" if passed else "FAIL",
        details=f"expected={expected}, actual={actual}",
        failures=failures,
    )