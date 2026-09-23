"""GOCOLL Validation Engine - Engine 4.

Runs the GOCOLL post-assembly checks through the shared
`ms_duke_je_common.assembly_gate` rule-pack gate. Tolerances and
check-enablement come from the `gocoll_v1` pack (override via
`GOCOLL_RULE_PACK`), so behaviour is configuration-driven and decoupled
from the legacy COG pydantic config.

Checks (gate participates in all enabled):
  - `balance`           - Sheet1 C2 SUMPRODUCT == 0 (hard gate)
  - `control_totals`    - every batch nets to 0 (hard gate)
  - `codeblock_completeness` - advisory: flags blank/no-GO-form lines (REVIEW)
  - `validation_tab`    - advisory: codes must exist in the master (REVIEW)
  - `reconciliation`    - advisory: A/B/C batch tie-out (REVIEW)
  - `line_count`        - optional COUNTA match (off by default)
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from decimal import Decimal

from ms_duke_je_common.assembly_gate import evaluate_gate, load_rule_pack
from ms_duke_je_common.extraction.worksheet_scan import coerce_decimal
from src.models.domain import CheckResult, ProcessingResult
from src.models.enums import CheckName
from src.models.gocoll_assembly_models import GoCollAssemblyResult
from src.models.gocoll_models import GoCollExtraction
from src.validation.gocoll_balance_check import check_balance
from src.validation.gocoll_codeblock_check import check_codeblock_completeness
from src.validation.gocoll_control_total_check import check_control_totals
from src.validation.gocoll_deriva_check import check_deriva_guard
from src.validation.gocoll_line_count_check import check_line_count
from src.validation.gocoll_reconciliation_check import check_reconciliation
from src.validation.gocoll_validation_tab_check import check_validation_tab

logger = logging.getLogger(__name__)

_RULE_PACK_NAME = os.getenv("GOCOLL_RULE_PACK", "gocoll_v1")


def validate_assembly(
    assembly: GoCollAssemblyResult,
    extraction: GoCollExtraction | None = None,
    *,
    wf_report=None,
    treasury_report=None,
    validation_master=None,
) -> ProcessingResult:
    """Engine 4 entry point - run the rule-pack-selected checks and gate.

    Args:
        assembly: Output from Engine 3 (the assembled GOCOLL journal).
        extraction: Optional Engine 1 output carrying the per-batch deposit
            controls used as a fallback by the `reconciliation` check.
        wf_report: Optional parsed :class:`WFReport` (the WF "B" control).
        treasury_report: Optional parsed :class:`TreasuryReport` (the "C"
            control + BAI 566 return items).
        validation_master: Optional runtime :class:`ValidationTabMaster`
            (all dimensions) from the user-uploaded Validation Tab; when
            omitted the ``validation_tab`` check is skipped.

    Returns:
        ProcessingResult with VERIFIED or FAILED status.
    """
    pack = load_rule_pack(_RULE_PACK_NAME)
    logger.info("Engine 4 (GOCOLL): rule_pack=%s v%s", pack.name, pack.version)

    validation_tab_result: CheckResult | None = None

    def _run_validation_tab() -> CheckResult:
        nonlocal validation_tab_result
        validation_tab_result = check_validation_tab(assembly, validation_master)
        return validation_tab_result

    def _run_derivation_follow_up() -> CheckResult:
        failures = (
            list(validation_tab_result.failures)
            if validation_tab_result is not None
            else []
        )
        return check_deriva_guard(assembly, failures)

    # (rule-pack check name, runner taking the assembly + the rule pack)
    dispatch: list[tuple[str, Callable[[], CheckResult]]] = [
        (CheckName.BALANCE, lambda: check_balance(
            assembly, tolerance=coerce_decimal(pack.threshold(CheckName.BALANCE, "tolerance"), default=Decimal("0.00")))),
        (CheckName.CONTROL_TOTALS, lambda: check_control_totals(
            assembly, tolerance=coerce_decimal(pack.threshold(CheckName.CONTROL_TOTALS, "tolerance"), default=Decimal("0.00")))),
        (CheckName.CODEBLOCK_COMPLETENESS, lambda: check_codeblock_completeness(assembly)),
        (CheckName.VALIDATION_TAB, _run_validation_tab),
        (CheckName.DERIVA_GUARD, _run_derivation_follow_up),
        (CheckName.RECONCILIATION, lambda: check_reconciliation(
            assembly, extraction,
            wf_report=wf_report,
            treasury_report=treasury_report,
            tolerance=coerce_decimal(pack.threshold(CheckName.RECONCILIATION, "tolerance"), default=Decimal("0.01")))),
        (CheckName.LINE_COUNT, lambda: check_line_count(
            assembly, expected=pack.threshold(CheckName.LINE_COUNT, "expected"))),
    ]

    local_checks: list[CheckResult] = []

    for check_name, runner in dispatch:
        if not pack.is_enabled(check_name):
            continue
        local_checks.append(runner())

    shared_result = evaluate_gate(
        local_checks,
        required_checks=pack.required_checks or None,
    )

    result = ProcessingResult(
        status=shared_result.status,
        checks=local_checks,
        failure_reasons=list(shared_result.failure_reasons),
        timestamp=shared_result.timestamp,
    )

    logger.info(
        "Engine 4 (GOCOLL) complete: status=%s, %d checks, %d passed, %d failed",
        result.status,
        len(local_checks),
        sum(1 for c in local_checks if c.passed),
        sum(1 for c in local_checks if not c.passed),
    )
    return result