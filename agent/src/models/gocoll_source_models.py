"""GOCOLL source-feed models — WF Transaction Report + Treasury Report.

These are the runtime bank/treasury feeds ingested alongside the lockbox batch
PDFs and the Validation Tab workbook (see the redesigned GOCOLL flow):

* :class:`WFReport`      — the Wells Fargo lockbox "Transactions Report"
                           (per-transaction rows with a batch number + gross check amount). Supplies
                           the **B (WF)** control total per batch for reconciliation.
* :class:`TreasuryReport` — the bank "Total Bank Transactions" feed
                           (Company/Account/Category/BAI/Debit/Credit). Supplies the **C (Treasury)**
                           control: lockbox deposits (BAI 115) and return items (BAI 566).

All monetary values are :class:`~decimal.Decimal`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from functools import cached_property

# Bank Administration Institute (BAI) transaction type codes used by GOCOLL.
BAI_LOCKBOX_DEPOSIT = "115"
BAI_RETURN_ITEM = "566"


# — Wells Fargo Transaction Report ——————————————————————————————————————

@dataclass
class WFTransaction:
    """One row of the WF lockbox Transactions Report."""

    batch_number: str
    check_number: str = ""
    check_amount: Decimal = Decimal("0")      # gross check amount as deposited
    transaction_total: Decimal = Decimal("0")
    transaction_number: str = ""
    deposit_date: str = ""
    site: str = ""
    lockbox: str = ""
    number_of_items: int = 0
    status: str = ""
    note: str = ""


@dataclass
class WFReport:
    """Parsed Wells Fargo lockbox Transactions Report (all batches)."""

    transactions: list[WFTransaction] = field(default_factory=list)
    source_file: str = ""

    @property
    def loaded(self) -> bool:
        return bool(self.transactions)

    @property
    def batch_numbers(self) -> list[str]:
        seen: dict[str, None] = {}
        for t in self.transactions:
            seen.setdefault(t.batch_number, None)
        return list(seen.keys())

    def transactions_for(self, batch_number: str) -> list[WFTransaction]:
        key = str(batch_number).strip()
        return [t for t in self.transactions if t.batch_number == key]

    def batch_check_total(self, batch_number: str) -> Decimal:
        """Gross check-amount total for a batch (the WF `B` control)."""
        return self._batch_totals_map.get(str(batch_number).strip(), Decimal("0"))

    def batch_transaction_total(self, batch_number: str) -> Decimal:
        """Gross transaction-total for a batch — the fallback `B` control.

        The register carries both a Check Amount and a Transaction Total column. They
        agreed on all 364 rows across January and February, so this normally returns
        the same figure; it exists so a file where they diverge is reconciled against
        the column that ties rather than reported as an unexplained difference.
        """
        return self._batch_txn_totals_map.get(str(batch_number).strip(), Decimal("0"))

    @cached_property
    def _batch_txn_totals_map(self) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for t in self.transactions:
            amount = t.transaction_total or t.check_amount
            totals[t.batch_number] = totals.get(t.batch_number, Decimal("0")) + amount
        return totals

    @cached_property
    def _batch_totals_map(self) -> dict[str, Decimal]:
        """Pre-compute all batch totals in a single O(n) pass.

        Falls back to `transaction_total` for a row with no Check Amount. The two
        columns agreed on all 364 register rows across January and February, so this
        changes nothing today; it exists so a blank Check Amount cell cannot silently
        drop a deposited check out of the bank control.
        """
        totals: dict[str, Decimal] = {}
        for t in self.transactions:
            amount = t.check_amount or t.transaction_total
            totals[t.batch_number] = totals.get(t.batch_number, Decimal("0")) + amount
        return totals

    def batch_totals(self) -> dict[str, Decimal]:
        return self._batch_totals_map


# — Treasury "Total Bank Transactions" feed ——————————————————————————————

@dataclass
class TreasuryTransaction:
    """One row of the Treasury bank-transaction feed."""

    company: str = ""
    account: str = ""
    category: str = ""
    transaction_date: str = ""
    bai_code: str = ""
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    description: str = ""
    transaction_description: str = ""


@dataclass
class TreasuryReport:
    """Parsed Treasury bank feed with header tie-out totals.

    `total_over_the_counter` / `total_return_items` are read from the report
    header banner; the per-BAI helpers aggregate the transaction rows. A nonzero
    OTC total creates balanced Sheet1 cash/detail lines and an analyst review
    flag because the negative detail's accounting must be confirmed before posting.
    """

    transactions: list[TreasuryTransaction] = field(default_factory=list)
    total_over_the_counter: Decimal = Decimal("0")
    total_return_items: Decimal = Decimal("0")
    source_file: str = ""

    @property
    def loaded(self) -> bool:
        return bool(self.transactions) or bool(
            self.total_return_items or self.total_over_the_counter
        )

    def rows_for_bai(self, bai_code: str) -> list[TreasuryTransaction]:
        key = str(bai_code).strip()
        return [t for t in self.transactions if t.bai_code == key]

    def credit_for_bai(self, bai_code: str) -> Decimal:
        return sum((t.credit for t in self.rows_for_bai(bai_code)), Decimal("0"))

    def debit_for_bai(self, bai_code: str) -> Decimal:
        return sum((t.debit for t in self.rows_for_bai(bai_code)), Decimal("0"))

    @property
    def return_items_total(self) -> Decimal:
        """Return-item total (BAI 566 credits), falling back to the header total."""
        rows_total = self.credit_for_bai(BAI_RETURN_ITEM)
        return rows_total if rows_total else self.total_return_items

    @property
    def lockbox_deposit_total(self) -> Decimal:
        """Total lockbox deposits booked by the bank (BAI 115 debits)."""
        return self.debit_for_bai(BAI_LOCKBOX_DEPOSIT)