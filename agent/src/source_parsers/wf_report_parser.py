"""Parse the Wells Fargo lockbox "Transactions Report" into a :class:`WFReport`.

The report is a flat table (one header row, then one row per deposited check):

    Transaction Number | Status | Note | Transaction Total | Deposit Date |
    Batch Number | Check Number | Check Amount | Site | Lockbox | Number of Items

The sheet is selected by header content and read in streaming mode. Iteration
stops after the table's Batch Number key stays blank, avoiding inflated Excel
dimensions that can otherwise force a scan of more than one million rows.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ms_duke_je_common.extraction.worksheet_scan import coerce_decimal
from ms_duke_je_common.normalization.extraction.workbook_reader import (
    match_sheet,
    match_sheet_by_columns,
    open_workbook,
)

from src.models.gocoll_source_models import WFReport, WFTransaction
from src.utils import strip_dot_zero

logger = logging.getLogger(__name__)

_SHEET_PATTERN = "*wf*transaction*"
_REQUIRED_COLUMNS = ["Batch Number", "Check Amount"]

HEADERS = {
    "transaction_number": "Transaction Number",
    "status": "Status",
    "note": "Note",
    "transaction_total": "Transaction Total",
    "deposit_date": "Deposit Date",
    "batch_number": "Batch Number",
    "check_number": "Check Number",
    "check_amount": "Check Amount",
    "site": "Site",
    "lockbox": "Lockbox",
    "number_of_items": "Number of Items",
}


def _clean_id(value: object) -> str:
    """Stringify a batch/check id, dropping a trailing `.0` on numeric cells."""
    if value is None:
        return ""
    return strip_dot_zero(str(value).strip())


def _normalise_header(value: object) -> str:
    return str(value or "").strip().lower().replace("_", " ")


def parse_wf_report(
    path: str | Path,
    *,
    sheet_name: str | None = None,
    blank_run_stop: int = 1000,
) -> WFReport:
    """Parse the WF Transactions Report workbook into a :class:`WFReport`."""
    p = Path(path)
    wb = open_workbook(p)
    transactions: list[WFTransaction] = []
    try:
        target = (
            sheet_name
            or match_sheet_by_columns(wb, _SHEET_PATTERN, _REQUIRED_COLUMNS)
            or match_sheet(wb, _SHEET_PATTERN)
            or wb.sheetnames[0]
        )
        worksheet = wb[target]
        header_map: dict[str, int] = {}
        header_row = 0
        expected = {
            _normalise_header(header): key for key, header in _HEADERS.items()
        }
        for index, row in enumerate(
            worksheet.iter_rows(min_row=1, max_row=15, values_only=True),
            start=1,
        ):
            resolved = {
                expected[normalised]: column
                for column, value in enumerate(row)
                if (normalised := _normalise_header(value)) in expected
            }
            if "batch_number" in resolved and "check_amount" in resolved:
                header_map = resolved
                header_row = index
                break

        if not header_map:
            logger.warning("WF report headers not found in %s", p.name)
            return WFReport(source_file=str(p))

        max_column = max(header_map.values()) + 1
        blank_run = 0
        for row in worksheet.iter_rows(
            min_row=header_row + 1,
            max_col=max_column,
            values_only=True,
        ):
            def value(key: str):
                return row[header_map[key]] if key in header_map else None

            batch = _clean_id(value("batch_number"))
            if not batch.isdigit():
                blank_run += 1
                if blank_run >= blank_run_stop:
                    break
                continue
            blank_run = 0
            transactions.append(
                WFTransaction(
                    batch_number=batch,
                    check_number=_clean_id(value("check_number")),
                    check_amount=coerce_decimal(value("check_amount")),
                    transaction_total=coerce_decimal(value("transaction_total")),
                    transaction_number=_clean_id(value("transaction_number")),
                    deposit_date=value("deposit_date") or "",
                    site=str(value("site") or ""),
                    lockbox=_clean_id(value("lockbox")),
                    number_of_items=int(coerce_decimal(value("number_of_items"))),
                    status=str(value("status") or ""),
                    note=str(value("note") or ""),
                )
            )
    finally:
        wb.close()

    report = WFReport(transactions=transactions, source_file=str(p))
    logger.info(
        "Parsed WF report %s: %d transaction(s) across %d batch(es)",
        p.name, len(transactions), len(report.batch_numbers),
    )
    return report