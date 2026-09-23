"""GOCOLL assembly models — Engines 2-3 output.

A GOCOLL journal entry is built per batch: N **detail** lines (coded from
the GO-Collection-Forms, negative) plus one **cash/control** line whose
amount is ``-SUM(details)`` so the batch nets to zero. The whole eFIS
Sheet1 ``MonetaryAmount`` column therefore sums to exactly 0.

``GoCollJELine`` carries the full Sheet1 ``Jrnl Line`` dimension set
(columns I..AL) so the eFIS formatter / ERP template agent can render
every column without lookups.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class GoCollJELine:
    """One eFIS Sheet1 journal line (the ``Jrnl Line`` band, columns I..AL)."""

    line_seq: int
    line_kind: str                  # "cash" | "detail" | "plug"
    monetary_amount: Decimal        # Sheet1 I — signed (detail negative, cash positive)
    business_unit: str = ""
    account: str = ""
    resource_type: str = ""
    operating_unit: str = ""
    resp_center: str = ""
    project: str = ""
    activity_id: str = ""
    process: str = ""
    location: str = ""
    product: str = ""
    affiliate: str = ""
    alloc_pool: str = ""
    line_descr: str = ""

    # Provenance (not rendered to Sheet1)
    batch_number: str = ""
    classification: str = ""  # "go_form" | "no_go_form" | "cash" | "otc_cash" | "otc_pending"
    check_number: str = ""
    confidence: float = 1.0
    warnings: list[str] = field(default_factory=list)
    # Carried forward from extraction so the check-level tie-out and the Check
    # Tie-Out review sheet can work off the assembled JE alone, without reaching
    # back into Engine 1. ``check_amount`` is the WF-authoritative figure.
    check_amount: Decimal = Decimal("0")
    tie_status: str = ""  # see models.enums.TieStatus


@dataclass
class GoCollBatchEntry:
    """The assembled lines for one lockbox batch (cash line + details)."""

    batch_number: str
    cash_line: GoCollJELine | None = None
    detail_lines: list[GoCollJELine] = field(default_factory=list)
    is_otc: bool = False

    @property
    def lines(self) -> list[GoCollJELine]:
        """Cash line first, then detail lines (Sheet1 ordering)."""
        return ([self.cash_line] if self.cash_line else []) + self.detail_lines

    @property
    def detail_total(self) -> Decimal:
        return sum((line.monetary_amount for line in self.detail_lines), Decimal("0"))

    @property
    def batch_total(self) -> Decimal:
        """Cash + details - must be 0 for a balanced batch."""
        cash = self.cash_line.monetary_amount if self.cash_line else Decimal("0")
        return cash + self.detail_total


@dataclass
class GoCollJournalEntry:
    """One complete GOCOLL JE (header + all batch entries)."""

    journal_date: str
    ledger: str                     # Sheet1 A — e.g. "01/31/2026"
    reversal_code: str              # Sheet1 B
    header_descr: str               # Sheet1 C
    journal_bus_unit: str           # Sheet1 E — "LB 604205: JAN2025_BATCH:632-633"
    journal_mask: str               # Sheet1 F — "10900"
    source: str                     # Sheet1 G — "GOCOLL"
    batch_entries: list[GoCollBatchEntry] = field(default_factory=list)

    @property
    def all_lines(self) -> list[GoCollJELine]:
        lines: list[GoCollJELine] = []
        for be in self.batch_entries:
            lines.extend(be.lines)
        return lines

    @property
    def monetary_total(self) -> Decimal:
        """Sheet1 C2 (SUMPRODUCT of column I) - must be 0."""
        return sum((line.monetary_amount for line in self.all_lines), Decimal("0"))

    @property
    def line_count(self) -> int:
        """Sheet1 A2 (COUNTA of column I)."""
        return len(self.all_lines)


@dataclass
class GoCollAssemblyResult:
    """Complete assembly output — Engine 3."""

    journal_entry: GoCollJournalEntry | None = None
    efis_rows: list[dict] = field(default_factory=list)
    is_balanced: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)