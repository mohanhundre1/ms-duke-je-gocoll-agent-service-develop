"""GOCOLL Validation Tab master reference loader.

Loads the analyst "Validation Tab" master code lists - the authoritative
PeopleSoft dimension tables (Business Unit, Account, Resource Type, ...) - that
:mod:`src.validation.gocoll_validation_tab_check` validates every assembled
eFIS line against.

The master is provided at runtime by the user-uploaded Validation Tab
workbook (resolved by triage), loaded via
:func:`load_validation_tab_from_workbook`. When no Validation Tab is
uploaded the dimension check degrades to a no-op rather than aborting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from src.utils import normalize_account, strip_dot_zero

logger = logging.getLogger(__name__)


def _normalize_bu(value: str) -> str:
    """Normalize a Business Unit code for comparison (trim, drop trailing .0)."""
    return strip_dot_zero(str(value or "").strip())


def _normalize_account(value: str) -> str:
    """Normalize an Account code for comparison (shared eFIS 7-char form)."""
    return normalize_account(value)


def _normalize_code(value: str) -> str:
    """Normalize a generic dimension code (trim, drop trailing .0)."""
    return strip_dot_zero(str(value or "").strip())


@dataclass
class ValidationTabMaster:
    """Resolved master code lists from the analyst Validation Tab.

    ``business_units`` / ``*_accounts`` are the original BU + Account tables; the
    remaining dimension sets (populated only by the runtime workbook loader)
    carry the other PeopleSoft dimensions so every coded line can be validated.
    A dimension whose set is empty is treated as "not provided" and never flags.
    """

    business_units: frozenset[str] = field(default_factory=frozenset)
    active_accounts: frozenset[str] = field(default_factory=frozenset)
    inactive_accounts: frozenset[str] = field(default_factory=frozenset)
    resource_types: frozenset[str] = field(default_factory=frozenset)
    operating_units: frozenset[str] = field(default_factory=frozenset)
    resp_centers: frozenset[str] = field(default_factory=frozenset)
    projects: frozenset[str] = field(default_factory=frozenset)
    activities: frozenset[str] = field(default_factory=frozenset)
    processes: frozenset[str] = field(default_factory=frozenset)
    locations: frozenset[str] = field(default_factory=frozenset)

    @property
    def loaded(self) -> bool:
        """True when at least one master list was loaded."""
        return bool(
            self.business_units
            or self.active_accounts
            or self.inactive_accounts
            or self.resource_types
            or self.operating_units
            or self.resp_centers
            or self.projects
            or self.activities
            or self.processes
            or self.locations
        )

    def is_known_bu(self, bu: str) -> bool:
        return _normalize_bu(bu) in self.business_units

    def is_known_account(self, account: str) -> bool:
        code = _normalize_account(account)
        return code in self.active_accounts or code in self.inactive_accounts

    def is_active_account(self, account: str) -> bool:
        return _normalize_account(account) in self.active_accounts

    @staticmethod
    def _known(value: str, known: frozenset[str]) -> bool:
        """Lenient membership: an unloaded (empty) dimension never flags."""
        if not known:
            return True
        return _normalize_code(value) in known

    def is_known_resource_type(self, value: str) -> bool:
        return self._known(value, self.resource_types)

    def is_known_operating_unit(self, value: str) -> bool:
        return self._known(value, self.operating_units)

    def is_known_resp_center(self, value: str) -> bool:
        return self._known(value, self.resp_centers)

    def is_known_project(self, value: str) -> bool:
        return self._known(value, self.projects)

    def is_known_activity(self, value: str) -> bool:
        return self._known(value, self.activities)

    def is_known_process(self, value: str) -> bool:
        return self._known(value, self.processes)

    def is_known_location(self, value: str) -> bool:
        return self._known(value, self.locations)


# -- Runtime workbook loader (all dimensions) --------------------------------
# The analyst ships the Validation Tab as a workbook sheet where each PeopleSoft
# dimension is an independent vertical list occupying its own column block. The
# blocks are located from two stacked header rows: a "group" row (Business Unit /
# Account Number / Resource Type / ...) and a "sub-header" row (BU / Number /
# Project ID / Active or Inactive / ...). We resolve each block's key column by
# group label, then pick the code column within the block by its sub-header.

_GROUP_ROW_MARKER = "business unit"

# dimension attr -> (exact group label, sub-header needle for the code column)
_DIMENSION_SPECS: list[tuple[str, str, str]] = [
    ("business_units", "business unit", "bu"),
    ("resource_types", "resource type", "number"),
    ("operating_units", "oper unit", "oper"),
    ("resp_centers", "resp center", "number"),
    ("projects", "project", "project id"),
    ("activities", "activity id", "activity"),
    ("processes", "process", "process id"),
    ("locations", "location", "location"),
]

_ACCOUNT_GROUP = "account number"
_ACCOUNT_KEY_SUB = "account number"
_ACCOUNT_FLAG_SUB = "active"


def _norm_header(value) -> str:
    return str(value).strip().lower() if value is not None else ""


def _resolve_block_columns(
    group_row: tuple, sub_row: tuple
) -> tuple[dict[str, int], dict[str, int]]:
    """Return ({attr: key_col}, {"accounts_key":col, "accounts_flag": col}).

    Blocks are delimited by non-empty cells in the group row; within each block
    the code column is the sub-header cell matching the spec needle (else the
    block's first column).
    """
    group_cols = sorted(
        idx for idx, cell in enumerate(group_row) if _norm_header(cell)
    )

    def _block_end(start: int) -> int:
        for gc in group_cols:
            if gc > start:
                return gc
        return len(sub_row)

    def _find_group(label: str) -> int | None:
        for idx in group_cols:
            if _norm_header(group_row[idx]) == label:
                return idx
        return None

    def _key_in_block(start: int, end: int, needle: str) -> int:
        for c in range(start, min(end, len(sub_row))):
            if needle in _norm_header(sub_row[c]):
                return c
        return start

    dim_cols: dict[str, int] = {}
    for attr, group_label, key_sub in _DIMENSION_SPECS:
        gc = _find_group(group_label)
        if gc is None:
            continue
        dim_cols[attr] = _key_in_block(gc, _block_end(gc), key_sub)

    acct_cols: dict[str, int] = {}
    agc = _find_group(_ACCOUNT_GROUP)
    if agc is not None:
        end = _block_end(agc)
        acct_cols["key"] = _key_in_block(agc, end, _ACCOUNT_KEY_SUB)
        acct_cols["flag"] = _key_in_block(agc, end, _ACCOUNT_FLAG_SUB)
    return dim_cols, acct_cols


def load_validation_tab_from_workbook(
    path: str | Path,
    *,
    sheet_name: str | None = None,
    max_rows: int | None = None,
    blank_run_stop: int = 1000,
) -> ValidationTabMaster:
    """Load a :class:`ValidationTabMaster` (all dimensions) from a workbook.

    Args:
        path: The Validation Tab workbook (may be the combined GOCollections file).
        sheet_name: Sheet to read; defaults to the first sheet whose name
            contains "validation".
        max_rows: Optional cap on data rows scanned (test/perf guard).
        blank_run_stop: Stop after this many consecutive all-blank data rows.

    Returns:
        A populated master; an empty master (``loaded == False``) when the sheet
        or its headers cannot be resolved, so callers degrade gracefully.
    """
    from ms_duke_je_common.normalization.extraction.workbook_reader import open_workbook

    p = Path(path)
    wb = open_workbook(p)
    try:
        target = sheet_name
        if target is None:
            target = next(
                (s for s in wb.sheetnames if "validation" in s.lower()),
                wb.sheetnames[0],
            )
        ws = wb[target]

        # Locate the group-header row within the first rows.
        head = list(ws.iter_rows(min_row=1, max_row=15, values_only=True))
        g_row_i = next(
            (
                i for i, row in enumerate(head)
                if any(_norm_header(c) == _GROUP_ROW_MARKER for c in row)
            ),
            None,
        )
        if g_row_i is None or g_row_i + 1 >= len(head):
            logger.warning("Validation Tab: header block not found in %s", p.name)
            return ValidationTabMaster()

        group_row = head[g_row_i]
        sub_row = head[g_row_i + 1]
        dim_cols, acct_cols = _resolve_block_columns(group_row, sub_row)

        sets: dict[str, set[str]] = {attr: set() for attr, _, _ in _DIMENSION_SPECS}
        active_acc: set[str] = set()
        inactive_acc: set[str] = set()

        data_start = g_row_i + 3  # 1-based worksheet row (header rows are g+1, g+2)
        watched = [c for c in dim_cols.values()]
        if acct_cols:
            watched.append(acct_cols["key"])

        blank_run = 0
        scanned = 0
        for row in ws.iter_rows(min_row=data_start, values_only=True):
            scanned += 1
            if max_rows is not None and scanned > max_rows:
                break
            any_val = False

            for attr, col in dim_cols.items():
                if col < len(row):
                    code = _normalize_code(row[col]) if row[col] is not None else ""
                    if code:
                        sets[attr].add(code)
                        any_val = True

            if acct_cols:
                kcol = acct_cols["key"]
                fcol = acct_cols["flag"]
                acc = _normalize_account(row[kcol]) if kcol < len(row) and row[kcol] is not None else ""
                if acc:
                    any_val = True
                    flag = str(row[fcol]).strip().lower() if fcol < len(row) and row[fcol] is not None else ""
                    if flag == "inactive":
                        inactive_acc.add(acc)
                    else:
                        active_acc.add(acc)

            if any_val:
                blank_run = 0
            else:
                blank_run += 1
                if blank_run >= blank_run_stop:
                    break
    finally:
        wb.close()

    master = ValidationTabMaster(
        business_units=frozenset(sets.get("business_units", set())),
        active_accounts=frozenset(active_acc),
        inactive_accounts=frozenset(inactive_acc),
        resource_types=frozenset(sets.get("resource_types", set())),
        operating_units=frozenset(sets.get("operating_units", set())),
        resp_centers=frozenset(sets.get("resp_centers", set())),
        projects=frozenset(sets.get("projects", set())),
        activities=frozenset(sets.get("activities", set())),
        processes=frozenset(sets.get("processes", set())),
        locations=frozenset(sets.get("locations", set())),
    )
    logger.info(
        "Loaded Validation Tab (workbook) from %s: BU=%d acct=%d(+%d inactive) "
        "restype=%d operunit=%d respctr=%d project=%d activity=%d process=%d location=%d",
        p.name, len(master.business_units), len(master.active_accounts),
        len(master.inactive_accounts), len(master.resource_types),
        len(master.operating_units), len(master.resp_centers), len(master.projects),
        len(master.activities), len(master.processes), len(master.locations),
    )
    return master