"""GOCOLL Validation Tab check - Engine 4.

Validates every assembled eFIS line's Business Unit, Account, Resource Type,
Oper Unit, Resp Center, Project, Activity ID, Process, and Location against the
analyst "Validation Tab" master (PeopleSoft dimension tables), so an invalid or
*inactive* code is surfaced for analyst review **before** the file is uploaded
to eFIS (where it would otherwise reject). Dimensions whose master list was not
loaded are validated leniently (never flag).

This is an advisory (``REVIEW``) check, not a hard gate: a vision/fallback
misread should route the line to a human (HOTL) rather than block an
otherwise-balanced, postable journal. Unknown codes and inactive accounts
are reported separately so the analyst sees exactly what to confirm.
"""

from __future__ import annotations

import logging

from src.models.domain import CheckResult
from src.models.enums import ValidationStatus
from src.models.gocoll_assembly_models import GoCollAssemblyResult
from src.validation.validation_tab_master import ValidationTabMaster

logger = logging.getLogger(__name__)

# Each failure below is prefixed "Batch <n> seq <n> (<line_kind>): ...". The
# line_kind marker is what lets the reporting layers tell a coding error on an
# analyst-coded detail line apart from a master-data gap on the cash/control
# line the agent generates itself. Keep the two in step.
CASH_LINE_MARKER = "(cash)"


def split_findings_by_line_kind(failures: list[str]) -> tuple[list[str], list[str]]:
    """Split Validation Tab failures into (coded detail, generated cash line).

    A cash-line rejection means the Validation Tab master does not carry the code
    block the agent builds for the cash side - a master-data coverage gap, not a
    coding error a reviewer can fix on the line. Reporting them together
    overstates how much coded detail needs attention.
    """
    on_detail: list[str] = []
    on_cash: list[str] = []
    for failure in failures:
        (on_cash if CASH_LINE_MARKER in str(failure) else on_detail).append(str(failure))
    return on_detail, on_cash


def _present_dim(value) -> str:
    """Return a validation value, treating placeholder zero as missing/blank."""
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    return "" if text == "0" else text


def check_validation_tab(
    assembly: GoCollAssemblyResult,
    master: ValidationTabMaster | None = None,
) -> CheckResult:
    """Verify every line's dimensions exist in the Validation Tab master.

    Checks Business Unit, Account (known + active), Resource Type, Oper Unit,
    Resp Center, Project, Activity ID, Process, and Location against the loaded
    master.

    Args:
        assembly: Engine 3 output (the assembled GOCOLL journal).
        master: The runtime master loaded from the user-uploaded Validation
            Tab workbook. When ``None`` or empty the check is skipped.

    Returns:
        A :class:`CheckResult` - ``PASS`` when every dimension is a known,
        active code; ``REVIEW`` listing the lines that need analyst
        confirmation. When no Validation Tab was uploaded the check is skipped
        (``PASS`` with a note) so the run is not blocked.
    """
    je = assembly.journal_entry

    if master is None or not master.loaded:
        return CheckResult(
            check_name="validation_tab",
            status=ValidationStatus.PASS,
            details="Validation Tab not uploaded - dimension check skipped",
        )

    failures: list[str] = []
    checked = 0

    if je is not None:
        for line in je.all_lines:
            checked += 1
            where = f"Batch {line.batch_number} seq {line.line_seq} ({line.line_kind})"

            bu = _present_dim(line.business_unit)
            if bu and not master.is_known_bu(bu):
                failures.append(f"{where}: Business Unit {bu} not in Validation Tab")

            account = _present_dim(line.account)
            if account:
                if not master.is_known_account(account):
                    failures.append(f"{where}: Account {account} not in Validation Tab")
                elif not master.is_active_account(account):
                    failures.append(f"{where}: Account {account} is INACTIVE in Validation Tab")

            # Additional PeopleSoft dimensions - validated leniently: a dimension
            # whose master list was not loaded never flags (see ``_known``).
            resource_type = _present_dim(line.resource_type)
            if resource_type and not master.is_known_resource_type(resource_type):
                failures.append(f"{where}: Resource Type {resource_type} not in Validation Tab")

            operating_unit = _present_dim(line.operating_unit)
            if operating_unit and not master.is_known_operating_unit(operating_unit):
                failures.append(f"{where}: Oper Unit {operating_unit} not in Validation Tab")

            resp_center = _present_dim(line.resp_center)
            if resp_center and not master.is_known_resp_center(resp_center):
                failures.append(f"{where}: Resp Center {resp_center} not in Validation Tab")

            project = _present_dim(line.project)
            if project and not master.is_known_project(project):
                failures.append(f"{where}: Project {project} not in Validation Tab")

            activity_id = _present_dim(line.activity_id)
            if activity_id and not master.is_known_activity(activity_id):
                failures.append(f"{where}: Activity ID {activity_id} not in Validation Tab")

            if project and not activity_id:
                failures.append(f"{where}: Activity ID is required when Project ID is populated")
            elif activity_id and not project:
                failures.append(f"{where}: Project ID is required when Activity ID is populated")

            process = _present_dim(line.process)
            if process and not master.is_known_process(process):
                failures.append(f"{where}: Process {process} not in Validation Tab")

            location = _present_dim(line.location)
            if location and not master.is_known_location(location):
                failures.append(f"{where}: Location {location} not in Validation Tab")

    passed = len(failures) == 0
    if passed:
        logger.info("GOCOLL validation_tab PASS: %d line(s) checked against master", checked)
    else:
        logger.warning(
            "GOCOLL validation_tab REVIEW: %d of %d line(s) need analyst confirmation",
            len(failures), checked,
        )

    return CheckResult(
        check_name="validation_tab",
        status=ValidationStatus.PASS if passed else ValidationStatus.REVIEW,
        details=f"{checked} line(s) validated against Validation Tab master",
        failures=failures,
    )