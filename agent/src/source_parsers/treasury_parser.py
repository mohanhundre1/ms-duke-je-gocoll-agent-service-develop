"""Parse the Treasury "Total Bank Transactions" feed into a :class:`TreasuryReport`.

Layout (combined GOCollections workbook, sheet "Treasury Report"):

    row 0   banner:  Duke Total Bank Transactions ... | Total Over the Counter | <n> |
                     Total Return Items | <n>
    row 2   headers: Company | Account | Category | Transaction Date | BAI Code |
                     Debit | Credit | Description | Transaction Description
    row 3+  data

The transaction rows are extracted with the shared ``ms_duke_je_common``
schema-driven ``extract_sheet`` (header-name column resolution + typed
coercion). The banner tie-out totals are not tabular, so they are read with a
small label scan over the pre-header rows. Return items are BAI 566 (credits);
lockbox deposits are BAI 115 (debits). OTC is captured for reference only.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path

from ms_duke_je_common.extraction.worksheet_scan import coerce_decimal
from ms_duke_je_common.normalization.extraction_sheet_extractor import extract_sheet
from ms_duke_je_common.normalization.extraction_workbook_reader import (
    match_sheet,
    match_sheet_by_columns,
    open_workbook,
)
from ms_duke_je_common.normalization.models.schema_models import (
    FieldDef,
    FieldType,
    HeaderMatchRule,
    HeaderScanDef,
    SheetDef,
)

from src.models.gocoll_source_models import TreasuryReport, TreasuryTransaction
from src.utils import strip_dot_zero

logger = logging.getLogger(__name__)

_SHEET_PATTERN = ".*treasury.*"
_REQUIRED_COLUMNS = ["BAI Code", "Debit"]
_BANNER_SCAN_ROWS = 3


def _field(name: str, header: str, ftype: FieldType = FieldType.STRING) -> FieldDef:
    return FieldDef(
        name=name,
        type=ftype,
        header_match=[HeaderMatchRule(priority=1, values=[header])],
    )


_SHEET_DEF = SheetDef(
    id="treasury",
    pattern=".*",
    header_scan=HeaderScanDef(max_row=15, max_col=40),
    data_starts_after_header=True,
    key_field="bai_code",
    key_filters="numeric_string",
    fields=[
        _field("company", "Company"),
        _field("account", "Account"),
        _field("category", "Category"),
        _field("transaction_date", "Transaction Date"),
        _field("bai_code", "BAI Code"),
        _field("debit", "Debit", FieldType.DECIMAL),
        _field("credit", "Credit", FieldType.DECIMAL),
        _field("transaction_description", "Transaction Description"),
        _field("description", "Description"),
    ],
)


def _norm(value: object) -> str:
    return str(value).strip().lower() if value is not None else ""


def _clean_str(value: object) -> str:
    if value is None:
        return ""
    return strip_dot_zero(str(value).strip())


def _next_value(row: tuple, start: int):
    for j in range(start + 1, len(row)):
        if row[j] is not None and str(row[j]).strip() != "":
            return row[j]
    return None


def _scan_banner_totals(ws) -> tuple[Decimal, Decimal]:
    """Read (total_otc, total_return_items) from the pre-header banner rows."""
    otc = None
    ret = None
    for row in ws.iter_rows(min_row=1, max_row=_BANNER_SCAN_ROWS, values_only=True):
        for idx, cell in enumerate(row):
            label = _norm(cell)
            if not label:
                continue
            if "over the counter" in label and otc is None:
                otc = _next_value(row, idx)
            elif "return item" in label and ret is None:
                ret = _next_value(row, idx)
    return (
        coerce_decimal(otc) if otc is not None else Decimal("0"),
        coerce_decimal(ret) if ret is not None else Decimal("0"),
    )


def parse_treasury_report(path: str | Path, *, sheet_name: str | None = None) -> TreasuryReport:
    """Parse the Treasury bank feed workbook into a :class:`TreasuryReport`."""
    p = Path(path)
    wb = open_workbook(p)
    try:
        target = (
            sheet_name
            or match_sheet_by_columns(wb, _SHEET_PATTERN, _REQUIRED_COLUMNS)
            or match_sheet(wb, _SHEET_PATTERN)
            or wb.sheetnames[0]
        )
        ws = wb[target]
        total_otc, total_return = _scan_banner_totals(ws)
        rows, _errors, _audit = extract_sheet(ws, _SHEET_DEF, target)
    finally:
        wb.close()

    transactions: list[TreasuryTransaction] = []
    for r in rows:
        bai = _clean_str(r.get("bai_code"))
        company = r.get("company") or ""
        if not (bai or company):
            continue
        transactions.append(
            TreasuryTransaction(
                company=company,
                account=_clean_str(r.get("account")),
                category=r.get("category") or "",
                transaction_date=(r.get("transaction_date") or ""),
                bai_code=bai,
                debit=r.get("debit") or Decimal("0"),
                credit=r.get("credit") or Decimal("0"),
                description=r.get("description") or "",
                transaction_description=r.get("transaction_description") or "",
            )
        )

    report = TreasuryReport(
        transactions=transactions,
        total_over_the_counter=total_otc,
        total_return_items=total_return,
        source_file=str(p),
    )
    logger.info(
        "Parsed Treasury report %s: %d row(s), return_items=%s, lockbox_deposits=%s, OTC(banner)=%s",
        p.name, len(transactions), report.return_items_total,
        report.lockbox_deposit_total, report.total_over_the_counter,
    )
    return report