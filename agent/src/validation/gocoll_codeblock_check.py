"""GOCOLL code-block completeness check — advisory blank/no-GO-form flagger.

GOCOLL is fully GO-form driven: a line with a missing dimension is left blank
and a check with no GO form still posts with an all-blank code block. Neither
is fabricated or excluded, so this check no longer hard-fails the gate — it
surfaces those lines as REVIEW items (routed to the JE Flags tab) so an analyst
completes them before posting.
"""

from __future__ import annotations

import logging

from src.models.domain import CheckResult
from src.models.enums import ValidationStatus
from src.models.gocoll_assembly_models import GoCollAssemblyResult

logger = logging.getLogger(__name__)

_REQUIRED = (("business_unit", "BusinessUnit"), ("account", "Account"),
             ("resource_type", "ResourceType"))

def check_codeblock_completeness(assembly: GoCollAssemblyResult) -> CheckResult:
    """Flag lines with no GO form or a blank mandatory dimension (advisory)."""
    je = assembly.journal_entry
    failures: list[str] = []

    if je is not None:
        for line in je.all_lines:
            if line.line_kind == "cash":
                continue
            if getattr(line, "classification", "") == "otc_pending":
                # A dedicated OTC review flag carries the SOP action. The blank
                # detail accounting is intentional until the analyst confirms it.
                continue
            if getattr(line, "classification", "") == "no_go_form":
                failures.append(
                    f"Batch {line.batch_number} seq {line.line_seq}: no GO form "
                    f"matched — posted with a blank code block"
                )
                continue
            missing = [label for attr, label in _REQUIRED
                       if not getattr(line, attr, "").strip()]
            if missing:
                failures.append(
                    f"Batch {line.batch_number} seq {line.line_seq}: "
                    f"missing {', '.join(missing)} (left blank)"
                )

    passed = len(failures) == 0
    if passed:
        logger.info("GOCOLL codeblock completeness PASS: %d line(s)",
                    je.line_count if je else 0)
    else:
        logger.warning("GOCOLL codeblock completeness: %d line(s) flagged for review",
                       len(failures))

    return CheckResult(
        check_name="codeblock_completeness",
        status=ValidationStatus.PASS if passed else ValidationStatus.REVIEW,
        details=(
            f"{je.line_count if je else 0} line(s) checked; "
            f"{len(failures)} with a blank mandatory dimension or no GO form"
        ),
        failures=failures,
    )