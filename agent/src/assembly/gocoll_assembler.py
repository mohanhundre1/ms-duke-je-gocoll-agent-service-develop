"""GOCOLL JE assembler - Engine 3.

Turns the classified transactions for a period into a balanced
:class:`GoCollJournalEntry` and the eFIS Sheet1 rows the ERP template
agent renders.

Per batch:
    - one **detail** line per classified transaction (signed negative - the
      GO-collection distribution / credit), and
    - one **cash/control** line whose amount is ``-SUM(details)`` (the
      positive lockbox deposit to cash account 0131356).

Because each batch's cash line offsets its details, the whole Sheet1
``MonetaryAmount`` column (C2 = SUMPRODUCT) sums to exactly 0 - the hard
success gate for the use case.

When Treasury reports a nonzero over-the-counter total, assembly also adds a
separate balanced OTC cash/detail pair. The negative detail dimensions remain
blank until an analyst confirms the accounting codeblock required by the SOP.

Cash-line and header constants are loaded from ``config/excel_format.yaml``
(mirrors the ITFI ``charge_types.yaml`` pattern); the dataclass defaults are
the JAN-2026 working-file values used as a fallback when the YAML is absent.
"""

from __future__ import annotations

import logging
from calendar import monthrange
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from functools import lru_cache
from pathlib import Path

import yaml

from src.classify.codeblock_classifier import ClassifiedLine, classify_transactions
from src.models.gocoll_assembly_models import (
    GoCollAssemblyResult,
    GoCollBatchEntry,
    GoCollJELine,
    GoCollJournalEntry,
)
from src.models.gocoll_models import GoCollExtraction
from src.utils import normalize_account

logger = logging.getLogger(__name__)

_CENTS = Decimal("0.01")

# -- eFIS format config (cash-line + header code-block values) ---------------
_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
_FORMAT_FILE = "excel_format.yaml"

@dataclass(frozen=True)
class ExcelFormat:
    """Cash-line + header constants for eFIS Sheet1 assembly.

    Field defaults are the JAN-2026 working-file values and act as the
    fallback when ``config/excel_format.yaml`` is missing or unreadable.
    """

    lockbox_number: str = "604205"
    # Header (eFIS Sheet1 columns A-H)
    header_bus_unit: str = "10900"
    journal_mask: str = "GOCOLL"
    source: str = "999"
    ledger: str = ""
    reversal_code: str = ""
    # Cash / control line (the positive lockbox deposit)
    cash_account: str = "0131356"
    cash_resource_type: str = "09810"
    cash_oper_unit: str = "9626"
    cash_resp_center: str = "9626"
    cash_bus_unit: str = "10900"
    cash_descr_template: str = "Batch {batch}"

@lru_cache(maxsize=1)
def load_excel_format() -> ExcelFormat:
    """Load the eFIS format config; fall back to defaults when absent."""
    path = _CONFIG_DIR / _FORMAT_FILE
    defaults = ExcelFormat()

    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        logger.warning("excel_format.yaml not loaded from %s - using defaults", path)
        return defaults

    header = data.get("header", {}) or {}
    cash = data.get("cash_line", {}) or {}
    return ExcelFormat(
        lockbox_number=str(data.get("lockbox_number", defaults.lockbox_number)),
        header_bus_unit=str(header.get("business_unit", defaults.header_bus_unit)),
        journal_mask=str(header.get("journal_mask", defaults.journal_mask)),
        source=str(header.get("source", defaults.source)),
        ledger=str(header.get("ledger", defaults.ledger)),
        reversal_code=str(header.get("reversal_code", defaults.reversal_code)),
        cash_account=str(cash.get("account", defaults.cash_account)),
        cash_resource_type=str(cash.get("resource_type", defaults.cash_resource_type)),
        cash_oper_unit=str(cash.get("oper_unit", defaults.cash_oper_unit)),
        cash_resp_center=str(cash.get("resp_center", defaults.cash_resp_center)),
        cash_bus_unit=str(cash.get("business_unit", defaults.cash_bus_unit)),
        cash_descr_template=str(cash.get("descr_template", defaults.cash_descr_template)),
    )

def _money(value: Decimal) -> Decimal:
    """Quantize to cents using banker's rounding."""
    return value.quantize(_CENTS, rounding=ROUND_HALF_EVEN)

def _detail_amount(line: ClassifiedLine) -> Decimal:
    """Signed Sheet1 amount for a detail line (distributions are negative).

    The extracted amount may arrive positive (as printed on the form); the
    Sheet1 detail line is the credit side, so we force it negative. There are
    no pre-signed plug lines anymore - every detail line is a form credit.
    """
    amt = _money(line.transaction.amount)
    return -abs(amt)

def _build_detail_line(line: ClassifiedLine, seq: int, batch_no: str) -> GoCollJELine:
    cb = line.code_block
    # The transcription is preserved on the code block (and in Extracted Data);
    # the posted line carries the eFIS form, which rejects an Account without
    # its leading zero.
    account = normalize_account(cb.account)
    warnings = list(line.warnings)
    transcribed = str(cb.account or "")
    if transcribed and account != transcribed:
        warnings.append(
            f"Account {transcribed} reformatted to {account} for eFIS"
        )
    return GoCollJELine(
        line_seq=seq,
        line_kind="detail",
        monetary_amount=_detail_amount(line),
        business_unit=cb.business_unit,
        account=account,
        resource_type=cb.resource_type,
        operating_unit=cb.operating_unit,
        resp_center=cb.resp_center,
        project=cb.project,
        activity_id=cb.activity_id,
        process=cb.process,
        location=cb.location,
        product=cb.product,
        affiliate=cb.affiliate,
        alloc_pool=cb.alloc_pool,
        line_descr=line.line_descr,
        batch_number=batch_no,
        classification=line.classification,
        check_number=line.transaction.check_number,
        confidence=line.transaction.confidence,
        warnings=warnings,
    )

def _build_cash_line(
    detail_total: Decimal, seq: int, batch_no: str, fmt: ExcelFormat
) -> GoCollJELine:
    """Cash/control line: amount = -SUM(details) (positive deposit)."""
    return GoCollJELine(
        line_seq=seq,
        line_kind="cash",
        monetary_amount=_money(-detail_total),
        business_unit=fmt.cash_bus_unit,
        account=normalize_account(fmt.cash_account),
        resource_type=fmt.cash_resource_type,
        operating_unit=fmt.cash_oper_unit,
        resp_center=fmt.cash_resp_center,
        line_descr=fmt.cash_descr_template.format(batch=batch_no),
        batch_number=batch_no,
        classification="cash",
    )

def assemble_batch(
    batch_number: str,
    classified: list[ClassifiedLine],
    seq_start: int,
    fmt: ExcelFormat,
) -> tuple[GoCollBatchEntry, int]:
    """Assemble one batch: cash line first, then detail lines.

    Returns the batch entry and the next available line sequence number.
    """
    # Build detail lines first to compute the cash offset.
    detail_lines: list[GoCollJELine] = []
    for cl in classified:
        # Placeholder seq; renumbered below so the cash line leads the batch.
        detail_lines.append(_build_detail_line(cl, 0, batch_number))

    detail_total = sum((dl.monetary_amount for dl in detail_lines), Decimal("0"))

    # Cash line leads, then details - contiguous Sheet1 ordering.
    cash_seq = seq_start + 1
    cash_line = _build_cash_line(detail_total, cash_seq, batch_number, fmt)
    for i, dl in enumerate(detail_lines, start=1):
        dl.line_seq = cash_seq + i

    entry = GoCollBatchEntry(
        batch_number=batch_number,
        cash_line=cash_line,
        detail_lines=detail_lines,
    )
    return entry, cash_seq + len(detail_lines)

def assemble_otc(
    amount: Decimal,
    seq_start: int,
    fmt: ExcelFormat,
) -> tuple[GoCollBatchEntry, int]:
    """Assemble a balanced OTC pair with accounting pending on the detail line."""
    otc_amount = _money(abs(amount))
    cash_seq = seq_start + 1
    detail_seq = cash_seq + 1
    detail_line = GoCollJELine(
        line_seq=detail_seq,
        line_kind="detail",
        monetary_amount=-otc_amount,
        business_unit="",
        account="",
        resource_type="",
        line_descr="OTC - Accounting Pending",
        batch_number="OTC",
        classification="otc_pending",
    )
    cash_line = _build_cash_line(
        detail_line.monetary_amount,
        cash_seq,
        "OTC",
        fmt,
    )
    cash_line.line_descr = "OTC Checks"
    cash_line.classification = "otc_cash"
    return (
        GoCollBatchEntry(
            batch_number="OTC",
            cash_line=cash_line,
            detail_lines=[detail_line],
            is_otc=True,
        ),
        detail_seq,
    )

def _efis_row(line: GoCollJELine, je: GoCollJournalEntry) -> dict:
    """Render one GoCollJELine to the 38-column eFIS Sheet1 dict.

    Header columns (A-H) repeat on every row; line columns (I-AL) carry the
    distribution. Key names match the COG eFIS formatter for ERP-template
    agent compatibility.
    """
    return {
        "A_journal_date": je.journal_date,
        "B_ledger": je.ledger,
        "C_reversal_code": je.reversal_code,
        "D_reversal_date": "",
        "E_header_descr": je.header_descr,
        "F_journal_bus_unit": je.journal_bus_unit,
        "G_journal_mask": je.journal_mask,
        "H_source": je.source,
        "I_monetary_amount": line.monetary_amount,
        "J_line_bus_unit": line.business_unit,
        "K_account": line.account,
        "L_resource_type": line.resource_type,
        "M_oper_unit": line.operating_unit,
        "N_resp_center": line.resp_center,
        "O_project": line.project,
        "P_activity_id": line.activity_id,
        "Q_process": line.process,
        "R_location": line.location,
        "S_product": line.product,
        "T_affiliate": line.affiliate,
        "U_alloc_pool": line.alloc_pool,
        "V_line_descr": line.line_descr,
        "W_material_id": "",
        "X_po_number": "",
        "Y_vendor": "",
        "Z_voucher": "",
        "AA_mat_stock_code": "",
        "AB_invoice": "",
        "AC_uom": "",
        "AD_quantity": "",
        "AE_statistics_cd": "",
        "AF_statistics_amt": "",
        "AG_memo_acct": "",
        "AH_puc": "",
        "AI_currency_cd": "",
        "AJ_exch_rate_type": "",
        "AK_unpost_seq": "",
        "AL_work_order": "",
    }

def assemble_journal_entry(
    extraction: GoCollExtraction,
    *,
    otc_amount: Decimal | None = None,
    ledger: str | None = None,
    reversal_code: str | None = None,
    journal_bus_unit: str | None = None,
    journal_mask: str | None = None,
    source: str | None = None,
    balance_tolerance: Decimal = Decimal("0.00"),
) -> GoCollAssemblyResult:
    """Engine 3 entry point - assemble the full GOCOLL journal entry.

    Args:
        extraction: Engine 1 output (batches of transactions).
        otc_amount: Optional Treasury over-the-counter deposit total. A nonzero
            amount adds a balanced cash/detail pair; the negative detail line's
            accounting dimensions remain blank for analyst completion.
        ledger / reversal_code / journal_bus_unit / journal_mask / source:
            eFIS header constants; each falls back to ``excel_format.yaml``
            when not explicitly overridden.
        balance_tolerance: Allowed |total| for the balance gate (default 0).

    Returns:
        A :class:`GoCollAssemblyResult` with the journal entry, eFIS rows,
        and the balance verdict.
    """
    fmt = load_excel_format()
    ledger = ledger if ledger is not None else fmt.ledger
    reversal_code = reversal_code if reversal_code is not None else fmt.reversal_code
    journal_bus_unit = journal_bus_unit if journal_bus_unit is not None else fmt.header_bus_unit
    journal_mask = journal_mask if journal_mask is not None else fmt.journal_mask
    source = source if source is not None else fmt.source

    result = GoCollAssemblyResult()
    je = GoCollJournalEntry(
        journal_date=_normalize_date(extraction.journal_date),
        ledger=ledger,
        reversal_code=reversal_code,
        header_descr=_header_descr(extraction, fmt),
        journal_bus_unit=journal_bus_unit,
        journal_mask=journal_mask,
        source=source,
    )

    seq = 0
    for batch in extraction.batches:
        classified = classify_transactions(batch.transactions)
        entry, seq = assemble_batch(batch.batch_number, classified, seq_start=seq, fmt=fmt)
        je.batch_entries.append(entry)

        # Per-batch warnings bubble up.
        for dl in entry.detail_lines:
            for w in dl.warnings:
                result.warnings.append(f"batch {batch.batch_number} seq {dl.line_seq}: {w}")

    if otc_amount and _money(abs(otc_amount)) != Decimal("0.00"):
        otc_entry, seq = assemble_otc(otc_amount, seq_start=seq, fmt=fmt)
        je.batch_entries.append(otc_entry)
        result.warnings.append(
            f"OTC amount {abs(otc_amount)} added as balanced cash/detail lines; "
            "negative detail accounting is blank pending analyst confirmation"
        )

    result.journal_entry = je
    result.efis_rows = [_efis_row(line, je) for line in je.all_lines]

    total = je.monetary_total
    result.is_balanced = abs(total) <= balance_tolerance
    if not result.is_balanced:
        result.errors.append(
            f"Journal does not balance: MonetaryAmount total = {total} (expected 0)"
        )

    logger.info(
        "GOCOLL assembly: %d batch(es), %d lines, total=%s, balanced=%s",
        len(je.batch_entries), je.line_count, total, result.is_balanced,
    )
    return result

_MONTH_ABBR = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)

def _period_prefix(journal_date: str) -> str:
    """Derive the ``MONYYYY`` period token (e.g. ``FEB2026``) from a journal date."""
    try:
        mm, _dd, yyyy = _normalize_date(journal_date).split("/")
        return f"{_MONTH_ABBR[int(mm) - 1]}{yyyy}"
    except (ValueError, IndexError):
        return ""

def _header_descr(extraction: GoCollExtraction, fmt: ExcelFormat) -> str:
    """Build the eFIS header description, e.g. ``LB 604205: FEB2026_BATCH:648-651``.

    Uses the supplied ``period_label`` when present; otherwise reconstructs the
    canonical ``{MONYYYY}_BATCH:{first}-{last}`` form from the journal date and
    batch numbers so the header never degrades to a period-less ``BATCH:...``.
    """
    lb = fmt.lockbox_number
    if extraction.period_label:
        return f"LB {lb}: {extraction.period_label}"
    batches = "-".join(extraction.batch_numbers)
    if not batches:
        return f"LB {lb}"
    period = _period_prefix(extraction.journal_date)
    if period:
        return f"LB {lb}: {period}_BATCH:{batches}"
    return f"LB {lb}: BATCH:{batches}"

def _normalize_date(value: str) -> str:
    """Normalize a journal date to month-end ``MM/DD/YYYY`` (eFIS Sheet1 col A).

    The GOCOLL JE posts on the last calendar day of its accounting period
    (matching the analyst working file, e.g. ``01/31/2026``). Accepts the
    UI month picker (``YYYY-MM``), ISO dates (``YYYY-MM-DD``) and
    already-formatted ``MM/DD/YYYY`` values; in every case the day is forced
    to month-end. Empty input stays empty (the agent never invents a date).
    """
    value = (value or "").strip()
    if not value:
        return ""

    year: int | None = None
    month: int | None = None
    try:
        if "-" in value:
            # YYYY-MM or YYYY-MM-DD
            parts = value.split("-")
            year, month = int(parts[0]), int(parts[1])
        elif "/" in value:
            # MM/DD/YYYY
            m, _d, y = value.split("/")
            year, month = int(y), int(m)
    except (ValueError, IndexError):
        return value  # unrecognised - pass through untouched

    if not year or not month or not (1 <= month <= 12):
        return value

    last_day = monthrange(year, month)[1]
    return f"{month:02d}/{last_day:02d}/{year:04d}"