"""GOCOLL domain models - extraction layer (Engine 1).

These dataclasses are the canonical units flowing out of the GOCOLL
extraction engine and into classification / assembly (Engines 2-3).

GOCOLL is document-extraction-dominant: a lockbox batch PDF is
split into per-transaction units. Each transaction carries:
- a *transaction summary* (lockbox/site/deposit-account/check/batch),
  read from the PDF text layer when present; and
- a *GO-Collection-Form code block* (the accounting distribution),
  read from the form image via GPT vision.

All monetary values use ``Decimal`` (ROUND_HALF_EVEN at assembly time).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


# -- Accounting code block (from the GO-Collection-Form image) ---------------

@dataclass
class GoCollCodeBlock:
    """The accounting distribution transcribed from a GO-Collection-Form.

    Maps 1:1 to the eFIS Sheet1 "Jrnl Line" dimension columns
    (J..AL). Empty string means "not present on the form".
    """

    business_unit: str = ""         # Sheet1 J - LineBusUnit, e.g. "10900"
    account: str = ""               # Sheet1 K - Account, 7-char string
    resource_type: str = ""         # Sheet1 L - ResourceType, e.g. "99810"
    operating_unit: str = ""        # Sheet1 M - OperUnit, e.g. "9626"
    resp_center: str = ""           # Sheet1 N - RespCenter, e.g. "9626"
    project: str = ""               # Sheet1 O - Project
    activity_id: str = ""           # Sheet1 P - ActivityID
    process: str = ""               # Sheet1 Q - Process
    location: str = ""              # Sheet1 R - Location
    product: str = ""               # Sheet1 S - Product
    affiliate: str = ""             # Sheet1 T - Affiliate
    alloc_pool: str = ""            # Sheet1 U - AllocPool

    def is_complete(self) -> bool:
        """True when the minimum dimensions to post a line are present."""
        return bool(self.business_unit and self.account and self.resource_type)


# -- Transaction summary (from the PDF text layer) ---------------------------

@dataclass
class GoCollTransaction:
    """A single lockbox check within a batch.

    The *summary* fields are read from the batch-PDF text layer (or vision
    OCR when the page is image-only). ``code_block`` is populated by the
    vision pass over the matching GO-Collection-Form image. ``amount`` is
    the signed distribution amount that will land in Sheet1 column I
    (negative for detail lines).
    """

    batch_number: str
    sequence: int                   # 1-based order within the batch
    check_number: str = ""          # Check# from the summary
    check_amount: Decimal = Decimal("0") # Check Amount (positive, as deposited)
    lockbox: str = ""               # e.g. "604205"
    site: str = ""                  # e.g. "CLT"
    deposit_account: str = ""       # e.g. "2000002929637"
    code_block: GoCollCodeBlock = field(default_factory=GoCollCodeBlock)
    amount: Decimal = Decimal("0")  # signed distribution amount (Sheet1 I)
    line_descr: str = ""            # Sheet1 V - LineDescr
    source_page: int | None = None  # PDF page the summary was read from
    extraction_tier: str = ""       # "text" | "vision"
    confidence: float = 1.0
    warnings: list[str] = field(default_factory=list)
    # Set when the coded GO-form distribution could not be reconciled to the
    # printed check total. The line still posts (the batch nets to zero via the
    # cash line = -SUM(details); no balancing plug is fabricated) but it is
    # routed to human review so a low-scan-quality misread never posts silently.
    needs_review: bool = False
    review_reason: str = ""


# -- Check (one deposited check = identifiers + N distribution lines) --------

@dataclass
class GoCollCheck:
    """One deposited check within a batch, assembled from its page(s).

    A single check may span 2-3 pages of the batch PDF (a summary / check-front
    page carrying ``check_amount`` plus one or more GO-form coding pages carrying
    the distribution ``lines``). The extraction coordinator groups those pages
    into this container by check number (or a batch/sequence fallback).
    """

    check_number: str = ""
    batch_number: str = ""
    sequence_number: str = ""
    check_amount: Decimal = Decimal("0") # deposited amount (from check/WF page)
    lines: list[GoCollTransaction] = field(default_factory=list)
    source_pages: list[int] = field(default_factory=list)

    # -- Bank fields, read from the Wells Fargo Transaction Summary block --
    # Vision reads; corroboration only. ``wf_check_amount`` (the WF Excel report) is
    # the authority for every tie-out comparison - comparing a coded total against a
    # vision-read bank amount produces a self-referential tie (batch 632 seq 10 was
    # recorded as tied at 115,589.65 when the page prints 33,766.95).
    transaction_total: Decimal = Decimal("0")
    transaction_type: str = ""      # "Regular" | "Check Only"

    # -- GO form section D "CHECK INFORMATION" --
    # Free text: one preparer writes the summed amount ("39,531.99"), another lists
    # each check ("$467.52 and $65.74"). Held as a list so the element count is a
    # direct multi-check signal and the sum is computed here, never by the model.
    form_check_amounts: list[Decimal] = field(default_factory=list)
    form_check_amount_raw: str = ""

    # -- Tie-out result (Engine 4) --
    wf_check_amount: Decimal | None = None  # AUTHORITATIVE, from the WF Excel report
    tie_status: str = ""                    # see TieStatus
    tie_variance: Decimal = Decimal("0")
    provenance: list[str] = field(default_factory=list)

    @property
    def coded_total(self) -> Decimal:
        """Sum of the absolute coded distribution amounts (section f)."""
        return sum((abs(ln.amount) for ln in self.lines), Decimal("0"))

    @property
    def declared_total(self) -> Decimal:
        """Sum of the amounts written in section D. Computed here, never by the LLM."""
        return sum(self.form_check_amounts, Decimal("0"))

    @property
    def deposited_amount(self) -> Decimal:
        """What was deposited for this check, falling back to the transaction total.

        A strict fallback, not an override: ``check_amount`` stands unless it is
        absent. The transaction total is the more reliable read (80 of 80 pages
        correct across the corpus), but substituting it wherever the two merely
        disagree would silently rewrite a figure the batch-level control is there to
        question. The swap belongs at the tie-out, where it can be justified by the
        result and recorded - see ``GoCollBatch.transaction_total_sum``.
        """
        return self.check_amount or self.transaction_total

    @property
    def deposit_amount_disputed(self) -> bool:
        """True when the two vision-read bank figures disagree.

        Never resolved silently: the tie-out uses ``deposited_amount`` but the
        disagreement stays visible so a reviewer can see which field was overridden.
        """
        return bool(
            self.transaction_total
            and self.check_amount
            and self.transaction_total != self.check_amount
        )

    @property
    def control_amount(self) -> Decimal:
        """The amount a tie-out must be measured against.

        Order: the WF Excel register, then the printed transaction total, then the
        section D check amount. The register is authoritative; the other two are
        vision reads used only when the check is absent from the report, which the
        caller records as ``wf_missing`` rather than treating as a clean tie.
        """
        if self.wf_check_amount is not None:
            return self.wf_check_amount
        return self.deposited_amount


# -- Batch (one lockbox deposit batch = one cash line + N details) -----------

@dataclass
class GoCollBatch:
    """One lockbox deposit batch.

    Assembly (Engine 3) emits N detail lines (one per transaction) plus a
    single cash/control line whose amount is ``-SUM(details)`` so the
    batch nets to zero.
    """

    batch_number: str
    checks: list[GoCollCheck] = field(default_factory=list)
    source_pdf: str = ""            # filename the batch was read from
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Independent Wells Fargo / Treasury lockbox deposit control, computed from
    # the per-check ``check_amount`` values read off the batch PDF. Used by the
    # reconciliation check (as a fallback to the WF Excel report) to tie the
    # booked JE cash line to the bank deposit.
    wf_deposit_booked: Decimal | None = None  # sum of booked check amounts
    wf_deposit_gross: Decimal | None = None   # sum of all check amounts
    wf_return_items: Decimal | None = None    # return items (sourced from Treasury BAI 566)
    # Vision LLM usage accumulated across this batch's page/GO-form reads
    # (fed to the governance CostTracker + rate limiter at the executor).
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0

    @property
    def transactions(self) -> list[GoCollTransaction]:
        """Flat view of every distribution line across the batch's checks."""
        return [ln for c in self.checks for ln in c.lines]

    @property
    def transaction_count(self) -> int:
        return len(self.transactions)

    @property
    def detail_total(self) -> Decimal:
        """Sum of signed distribution amounts across detail transactions."""
        return sum((t.amount for t in self.transactions), Decimal("0"))

    @property
    def check_total(self) -> Decimal:
        """Sum of check amounts (as deposited) across the batch's checks."""
        return sum((c.deposited_amount for c in self.checks), Decimal("0"))

    @property
    def transaction_total_sum(self) -> Decimal:
        """Sum of the printed WF transaction totals - the fallback bank control.

        Used only when ``check_total`` fails to tie to the register. Reading the same
        deposit off a different field is the one independent second opinion available
        without going back to the image.
        """
        return sum((c.transaction_total for c in self.checks), Decimal("0"))

    @property
    def disputed_checks(self) -> list[GoCollCheck]:
        """Checks whose two vision-read bank figures disagree; for the review sheet."""
        return [c for c in self.checks if c.deposit_amount_disputed]


# -- Full extraction result (all batches for the period) ---------------------

@dataclass
class GoCollExtraction:
    """Aggregated Engine 1 output for a GOCOLL period."""

    period_label: str = ""
    journal_date: str = ""          # e.g. "JAN2025_BATCH:632-633"
    batches: list[GoCollBatch] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Vision LLM usage aggregated across every batch (governance/cost tracking).
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    vision_model: str = ""
    # Check-level reconciliation flag rows (split-aware; set pre-assembly so
    # digit-repair corrections flow into the JE). Consumed by the flags builder.
    check_recon_rows: list = field(default_factory=list)

    @property
    def batch_numbers(self) -> list[str]:
        return [b.batch_number for b in self.batches]

    @property
    def transaction_count(self) -> int:
        return sum(b.transaction_count for b in self.batches)

    @property
    def review_items(self) -> list[GoCollTransaction]:
        """Transactions flagged for human review (unreconciled GO-form read)."""
        return [
            t
            for b in self.batches
            for t in b.transactions
            if t.needs_review
        ]