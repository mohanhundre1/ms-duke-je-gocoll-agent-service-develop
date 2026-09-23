"""GOC OLL A/B/C batch reconciliation result models.

Per batch the flow ties three independent totals:

* **A = coded**  - the GO-form distribution total booked in the JE.
* **B = WF**     - the Wells Fargo Transactions Report gross check total.
* **C = Treasury** - the bank lockbox deposit (BAI 115) confirmed for the batch.

An A-vs-B gap that matches a Treasury return item (BAI 566) is *explained*
(the returned check is in the bank total but not coded); an unmatched gap is a
first-class reconciliation flag. OTC is on hold and not reconciled here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from functools import cached_property


# Per-batch reconciliation outcome.
STATUS_TIE = "tie"                  # A == B (within tolerance)
STATUS_RETURN_ITEM = "return_item"  # A-vs-B gap explained by a BAI 566 return item
STATUS_DIFFERENCE = "difference"    # unexplained A-vs-B gap (needs analyst review)
STATUS_NO_CONTROL = "no_control"    # no WF report supplied at all - cannot reconcile
STATUS_WF_MISSING = "wf_missing"    # WF report loaded but this batch is absent from it


@dataclass
class BatchReconciliation:
    """A/B/C tie-out for a single batch."""

    batch_number: str
    coded_amount: Decimal            # A
    wf_amount: Decimal | None = None     # B
    treasury_amount: Decimal | None = None  # C
    return_items: Decimal = Decimal("0")  # attributed BAI 566 return items
    status: str = STATUS_NO_CONTROL
    detail: str = ""

    @property
    def diff_coded_wf(self) -> Decimal | None:
        """A - B (negative when the bank total exceeds the coded total)."""
        if self.wf_amount is None:
            return None
        return self.coded_amount - self.wf_amount

    @property
    def diff_wf_treasury(self) -> Decimal | None:
        """B - C."""
        if self.wf_amount is None or self.treasury_amount is None:
            return None
        return self.wf_amount - self.treasury_amount

    @property
    def explained(self) -> bool:
        return self.status in (STATUS_TIE, STATUS_RETURN_ITEM, STATUS_NO_CONTROL)


@dataclass
class ReconciliationResult:
    """All per-batch tie-outs plus the Treasury totals used to explain gaps."""

    batches: list[BatchReconciliation] = field(default_factory=list)
    tolerance: Decimal = Decimal("0.01")
    treasury_return_items_total: Decimal = Decimal("0")
    treasury_otc_total: Decimal = Decimal("0")

    @cached_property
    def unexplained(self) -> list[BatchReconciliation]:
        return [b for b in self.batches if b.status == STATUS_DIFFERENCE]

    @cached_property
    def wf_missing(self) -> list[BatchReconciliation]:
        """Batches absent from a loaded WF report (previously masked by fallback)."""
        return [b for b in self.batches if b.status == STATUS_WF_MISSING]

    @cached_property
    def flagged(self) -> list[BatchReconciliation]:
        """All batches needing analyst attention: unexplained gaps + missing WF."""
        return self.unexplained + self.wf_missing

    @cached_property
    def return_item_batches(self) -> list[BatchReconciliation]:
        return [b for b in self.batches if b.status == STATUS_RETURN_ITEM]

    @cached_property
    def has_differences(self) -> bool:
        return bool(self.flagged)