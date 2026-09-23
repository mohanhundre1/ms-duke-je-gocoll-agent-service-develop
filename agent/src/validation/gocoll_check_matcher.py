"""GOCLL check-level matcher — pure logic, no I/O and no LLM.

Three responsibilities, all deterministic and unit-testable:

1. :func:`classify_form` – decide whether a GO form covers one check or several,
   by comparing the preparer's section D declaration against the bank's printed
   transaction total, and separately detecting a bad grid read.
2. :func:`needs_reread` – the domain rule "a multi-check form carries at least one
   code block per check", inverted into a detector for a dropped code block.
3. :func:`partition_lines_to_checks` – allocate coded lines to the batch's WF
   checks by contiguous subset-sum, so a multi-check form is split the way the
   analyst splits it.

Plus :func:`find_digit_correction`, the tightly-railed repair for a single-digit
vision misread.

Why section D is a *list*: the box is free text and preparers fill it two ways. Canal
Wood (batch 632) reads ``39,531.99``, the summed remittance; Suburban Propane (batch
650) reads ``$467.52 and $65.74``, itemised. Collapsing that to one number makes the
second form look like a single check and silently loses the second check.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from src.models.enums import FormClass, TieStatus

logger = logging.getLogger(__name__)

DEFAULT_TOLERANCE = Decimal("0.01")

# Guard for the DFS below. Batch 651 is 44 checks against 52 lines; without a bound
# a pathological pool of near-equal amounts explores exponentially.
DEFAULT_NODE_BUDGET = 200_000


# -- 1. Form classification --------------------------------------------------


def classify_form(
    coded_total: Decimal,
    declared_amounts: Sequence[Decimal],
    transaction_total: Decimal,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> FormClass:
    """Classify one GO form against the bank's transaction total.

    Order matters. The grid-defect test runs first: if the coded rows do not add up
    to what section D declares, the *grid read* is untrustworthy, so the form must
    never be routed into the split branch on the strength of it.

    Args:
        coded_total: Absolute sum of the section F coding rows as extracted.
        declared_amounts: Every amount transcribed from section D, unsummed.
        transaction_total: The bank's printed "Transaction Total" for this
            transaction. Equal to the printed "Check Amount" in all 364 observed
            WF rows; this lockbox is configured one check per transaction.
        tolerance: Absolute money tolerance.

    Returns:
        The :class:`FormClass` describing what the form represents.
    """
    declared = sum(declared_amounts, Decimal("0"))

    if not declared_amounts:
        return FormClass.UNDECLARED

    if coded_total and abs(coded_total - declared) > tolerance:
        return FormClass.GRID_DEFECT

    if len(declared_amounts) > 1:
        return FormClass.SPLIT

    if not transaction_total:
        return FormClass.UNDECLARED

    delta = declared - transaction_total
    if abs(delta) <= tolerance:
        return FormClass.SINGLE
    if delta > 0:
        return FormClass.SPLIT
    # The form declares less than the bank received. Not a multi-check form: there
    # is no residual to hunt for. A short/wrong form or a bad section D read.
    return FormClass.SHORT_FORM


def needs_reread(
    declared_amounts: Sequence[Decimal],
    transaction_total: Decimal,
    n_code_blocks: int,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> bool:
    """True when a claimed split is impossible given the number of code blocks read.

    A code block is never split across two checks, so a form covering N checks
    carries at least N code blocks. Both multi-check forms in the corpus satisfy
    this exactly (Canal Wood 2/2, Suburban Propane 2/2).

    That rule is used here as a *detector*, not as a precondition. Gating the
    section D comparison on ``n_code_blocks > 1`` would suppress the check precisely
    when a code block was dropped, which is the most common vision fault: one
    observed read returned 29 rows against a true 52. Inverted, the same rule says a
    section D discrepancy with too few blocks means the grid read is wrong.
    """
    declared = sum(declared_amounts, Decimal("0"))
    implied_checks = max(len(declared_amounts), 2 if declared - transaction_total > tolerance else 1)
    return implied_checks > 1 and n_code_blocks < implied_checks


# -- 2. Line-to-check partition -----------------------------------------------


@dataclass(frozen=True)
class Segment:
    """A contiguous run of coded lines belonging to one check."""

    start: int  # index into the ordered line list
    length: int
    check_number: str
    check_amount: Decimal

    @property
    def stop(self) -> int:
        return self.start + self.length


@dataclass
class PartitionResult:
    """Outcome of allocating coded lines to WF checks."""

    status: str = "ok"  # ok | ambiguous | undecidable | empty
    segments: list[Segment] = field(default_factory=list)
    unmatched_checks: list[tuple[str, Decimal]] = field(default_factory=list)
    unassigned_lines: list[int] = field(default_factory=list)
    nodes_visited: int = 0
    # A second, materially different way to split the same lines across the same
    # checks. Its presence means the split is a guess, however plausible.
    alternate: list[Segment] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def undecidable(self) -> bool:
        """Two or more valid splits exist; the arithmetic cannot choose between them."""
        return self.status == "undecidable"


def partition_lines_to_checks(
    line_amounts: Sequence[Decimal],
    checks: Sequence[tuple[str, Decimal]],
    tolerance: Decimal = DEFAULT_TOLERANCE,
    node_budget: int = DEFAULT_NODE_BUDGET,
) -> PartitionResult:
    """Split an ordered run of coded lines into per-check contiguous segments.

    The preparer enters one check at a time, so a batch's detail lines are an
    ordered sequence of contiguous per-check runs, each summing to exactly one
    deposited check amount.

    Amounts are consumed from a *multiset*: batch 651 carries three distinct checks
    at exactly ``42.00``, so which one a segment binds to is arbitrary but the totals
    are right. Callers that already know a line's check number should prefer it.

    Checks left over are returned rather than forced into the partition. A returned
    item or a MARBS-excluded receipt legitimately has no coded lines at all.

    Args:
        line_amounts: Absolute line amounts in source order.
        checks: ``(check_number, check_amount)`` from the WF report.
        tolerance: Absolute money tolerance when closing a segment.
        node_budget: DFS node cap before declaring the partition ambiguous.

    Returns:
        A :class:`PartitionResult`. ``status="ambiguous"`` means the search was
        abandoned and the caller should degrade rather than trust the segments.
    """
    amounts = [abs(Decimal(a)) for a in line_amounts]
    pool = [(str(num), abs(Decimal(amt))) for num, amt in checks]

    if not amounts:
        return PartitionResult(
            status="empty", unmatched_checks=list(pool),
        )

    # Index the pool by amount so a segment close is a dict hit, not a scan.
    by_amount: dict[Decimal, list[int]] = {}
    for idx, (_num, amt) in enumerate(pool):
        by_amount.setdefault(amt, []).append(idx)

    n = len(amounts)
    consumed = [False] * len(pool)
    solutions: list[list[Segment]] = []
    nodes = 0
    exhausted = False
    # (start index, frozenset of still-unconsumed pool indexes) -> already failed.
    seen: set[tuple[int, frozenset[int]]] = set()

    def remaining() -> frozenset[int]:
        return frozenset(i for i, done in enumerate(consumed) if not done)

    def dfs(start: int, acc: list[Segment]) -> bool:
        """Explore splits, stopping once a second distinct one proves ambiguity."""
        nonlocal nodes, exhausted
        if nodes >= node_budget:
            exhausted = True
            return False
        nodes += 1

        if start >= n:
            solutions.append(list(acc))
            return len(solutions) >= 2

        key = (start, remaining())
        if key in seen:
            return False

        found_before = len(solutions)
        running = Decimal("0")
        for end in range(start, n):
            running += amounts[end]
            # Close the segment against any unconsumed check of this amount.
            candidates = [
                i for amt, idxs in by_amount.items()
                if abs(running - amt) <= tolerance
                for i in idxs
                if not consumed[i]
            ]
            for pool_idx in candidates:
                consumed[pool_idx] = True
                acc.append(Segment(
                    start=start,
                    length=end - start + 1,
                    check_number=pool[pool_idx][0],
                    check_amount=pool[pool_idx][1],
                ))
                if dfs(end + 1, acc):
                    return True
                acc.pop()
                consumed[pool_idx] = False
                if exhausted:
                    return False
            # One binding per amount value is enough: the alternatives are
            # interchangeable duplicates (651's three 42.00 checks).
            break

        # Only memoize states that led nowhere. Memoizing a productive state would
        # hide the very alternatives this search exists to find.
        if len(solutions) == found_before:
            seen.add(key)
        return False

    dfs(0, [])

    if solutions:
        chosen = solutions[0]
        matched = {s.check_number for s in chosen}
        unmatched = [(num, amt) for num, amt in pool if num not in matched]
        if len(solutions) > 1:
            logger.warning(
                "GOCLL partition of %d lines is undecidable: %d distinct splits "
                "satisfy the same checks", n, len(solutions),
            )
            return PartitionResult(
                status="undecidable",
                segments=chosen,
                alternate=solutions[1],
                unmatched_checks=unmatched,
                nodes_visited=nodes,
            )
        return PartitionResult(
            status="ok",
            segments=chosen,
            unmatched_checks=unmatched,
            nodes_visited=nodes,
        )

    if exhausted:
        logger.warning(
            "GOCLL partition abandoned after %d nodes (%d lines, %d checks) — "
            "degrading to greedy 1:1",
            nodes, n, len(pool),
        )

    greedy = _greedy_fallback(amounts, pool, tolerance)
    greedy.status = "ambiguous"
    greedy.nodes_visited = nodes
    return greedy


def _greedy_fallback(
    amounts: list[Decimal],
    pool: list[tuple[str, Decimal]],
    tolerance: Decimal,
) -> PartitionResult:
    """Best-effort 1:1 binding by descending amount when the DFS cannot solve."""
    taken = [False] * len(pool)
    segments: list[Segment] = []
    unassigned: list[int] = []

    order = sorted(range(len(amounts)), key=lambda i: amounts[i], reverse=True)
    for line_idx in order:
        hit = next(
            (
                i for i, (_num, amt) in enumerate(pool)
                if not taken[i] and abs(amounts[line_idx] - amt) <= tolerance
            ),
            None,
        )
        if hit is None:
            unassigned.append(line_idx)
            continue
        taken[hit] = True
        segments.append(Segment(
            start=line_idx, length=1,
            check_number=pool[hit][0], check_amount=pool[hit][1],
        ))

    segments.sort(key=lambda s: s.start)
    return PartitionResult(
        segments=segments,
        unmatched_checks=[p for i, p in enumerate(pool) if not taken[i]],
        unassigned_lines=sorted(unassigned),
    )


# -- 3. Single-digit misread repair -------------------------------------------


def _digits(value: Decimal) -> str:
    return str(abs(value)).replace(".", "").lstrip("0") or "0"


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (ca != cb),
            ))
        prev = cur
    return prev[-1]


def digit_similarity(a: Decimal, b: Decimal) -> float:
    """1.0 for identical digit strings, falling to 0.0 as they diverge.

    Proportional by design: at four digits a 0.55 floor permits one edit, at two
    digits it demands an exact match. Short amounts carry less evidence, so they
    should have to agree more.
    """
    da, db = _digits(a), _digits(b)
    return 1.0 - _levenshtein(da, db) / max(len(da), len(db))


# Calibrated against the six known batches. Genuine misreads scored 0.83 / 0.75 /
# 0.67 and beat their nearest rival by 0.50 / 0.50 / 0.38. Structural failures
# (dropped rows, duplicated rows) scored at most 0.44 and beat nothing — their best
# "match" was no better than the field, and in one case worse. Both rails separate
# the two populations cleanly on their own; requiring both is defence in depth.
MIN_SIMILARITY = 0.55
MAX_EDITS = 2
MIN_DOMINANCE = 0.15


def find_digit_correction(
    coded_amount: Decimal,
    candidate_checks: Sequence[tuple[str, Decimal]],
    tolerance: Decimal = DEFAULT_TOLERANCE,
    min_similarity: float = MIN_SIMILARITY,
    max_edits: int = MAX_EDITS,
    min_dominance: float = MIN_DOMINANCE,
) -> tuple[str, Decimal] | None:
    """Find the leftover WF check a misread coded amount was meant to be.

    This is not a similarity search. The register fixes the candidate value, so the
    only question is whether substituting it is plausibly a *misread* rather than a
    coincidence. Four rails, all required:

    1. at most ``max_edits`` Levenshtein edits over the digit string, counting
       substitution, insertion and deletion
    2. ``digit_similarity`` at or above ``min_similarity``
    3. the best candidate beats the runner-up by ``min_dominance`` — on a real
       misread the right answer dominates; on a structural failure nothing does
    4. the candidate comes from the leftover **WF** pool, never a free number

    Deliberately *not* a rail: matching cents. Batch 648 read ``1,623.05`` as
    ``1,823.06``, so requiring identical cents would reject a genuine misread.

    Returns:
        ``(check_number, corrected_amount)``, or ``None`` when nothing qualifies.
    """
    scored: list[tuple[float, int, str, Decimal]] = []
    for num, amt in candidate_checks:
        if abs(amt - coded_amount) <= tolerance:
            continue  # already a tie; not a correction
        dist = _levenshtein(_digits(coded_amount), _digits(amt))
        scored.append((digit_similarity(coded_amount, amt), dist, num, amt))

    if not scored:
        return None

    scored.sort(key=lambda t: -t[0])
    best_sim, best_dist, best_num, best_amt = scored[0]

    if best_dist > max_edits or best_sim < min_similarity:
        return None
    if len(scored) > 1 and best_sim - scored[1][0] < min_dominance:
        logger.info(
            "GOCLL digit correction for %s ambiguous (%.2f vs %.2f) — skipped",
            coded_amount, best_sim, scored[1][0],
        )
        return None
    return best_num, best_amt


# -- 4. Batch reconciliation --------------------------------------------------


@dataclass(frozen=True)
class LineGroup:
    """Coded lines that came off one GO form (in practice, one source page).

    Grouping is the segmentation prior. A full-batch subset-sum over every line at
    once is both slower and less accurate than solving each form against the check
    register, because lines physically belong to the form they were coded on.
    """

    key: str  # page or form identifier, for reporting
    amounts: tuple[Decimal, ...]
    # Coded rows on this form whose amount did not read. The total can still tie
    # while the distribution is wrong, so this travels with the group.
    unpriced_lines: int = 0


@dataclass
class CheckOutcome:
    check_number: str
    check_amount: Decimal
    coded_total: Decimal = Decimal("0")
    tie_status: TieStatus = TieStatus.BREAK
    group_key: str = ""
    variance: Decimal = Decimal("0")
    note: str = ""
    plug: Decimal = Decimal("0")  # booked to Suspense/Misc to force the balance
    needs_review: bool = False


@dataclass
class GroupOutcome:
    key: str
    resolved: bool
    checks: list[str] = field(default_factory=list)
    unresolved_total: Decimal = Decimal("0")
    note: str = ""
    unpriced_lines: int = 0


@dataclass
class BatchReconciliation:
    outcomes: list[CheckOutcome] = field(default_factory=list)
    groups: list[GroupOutcome] = field(default_factory=list)
    coded_total: Decimal = Decimal("0")
    wf_total: Decimal = Decimal("0")

    @property
    def return_item_total(self) -> Decimal:
        return sum(
            (o.check_amount for o in self.outcomes
             if o.tie_status is TieStatus.RETURN_ITEM),
            Decimal("0"),
        )

    @property
    def control_total(self) -> Decimal:
        """The deposit control: the register less anything that bounced.

        A returned item is listed by the register but never deposited, so it is not
        part of what the JE has to account for. Confirmed against the analyst
        workbook: batch 632's control is 159,067.49, being the 174,377.89 register
        less the 15,310.40 return item.
        """
        return self.wf_total - self.return_item_total

    @property
    def variance(self) -> Decimal:
        return self.coded_total - self.control_total

    @property
    def booked_total(self) -> Decimal:
        """What the JE will actually carry once snaps and plugs are applied."""
        return sum((o.coded_total + o.plug for o in self.outcomes), Decimal("0"))

    @property
    def plug_total(self) -> Decimal:
        return sum((o.plug for o in self.outcomes), Decimal("0"))

    @property
    def balanced(self) -> bool:
        """True when the booked JE equals the control. Says nothing about accuracy."""
        return abs(self.booked_total - self.control_total) <= DEFAULT_TOLERANCE

    @property
    def review_items(self) -> list[CheckOutcome]:
        return [o for o in self.outcomes if o.needs_review]

    @property
    def tied(self) -> list[CheckOutcome]:
        return [o for o in self.outcomes if o.tie_status in
                (TieStatus.TIE, TieStatus.SPLIT, TieStatus.CORRECTED)]

    @property
    def fully_explained(self) -> bool:
        """True when no check and no coded line is left unaccounted for.

        ``NO_FORM`` counts as unexplained. A check the register lists but no form
        covers is either a return item or a form we failed to read, and until the
        two are told apart it is a gap, not a clean result.
        """
        return not [
            o for o in self.outcomes
            if o.tie_status in (
                TieStatus.BREAK, TieStatus.NO_CONTROL, TieStatus.NO_FORM,
            )
        ] and all(g.resolved for g in self.groups)

    @property
    def understated_lines(self) -> int:
        """Coded rows booked at zero because their amount did not read.

        Invisible to every tie-out control: the check still ties, the register still
        agrees, and the money simply lands on the wrong distribution lines.
        """
        return sum(g.unpriced_lines for g in self.groups)

    @property
    def trustworthy(self) -> bool:
        """Balanced, fully explained, and distributing every line it booked."""
        return self.balanced and self.fully_explained and not self.understated_lines


def _best_assignment(
    groups: Sequence[LineGroup],
    checks: Sequence[tuple[str, Decimal]],
) -> list[tuple[LineGroup, tuple[str, Decimal]]]:
    """Pair leftover groups to leftover checks, minimising total absolute gap.

    Exhaustive for the small counts this ever sees (at most a handful of residuals
    per batch), greedy beyond that.
    """
    from itertools import permutations

    n = min(len(groups), len(checks))
    if not n:
        return []
    totals = [sum(g.amounts, Decimal("0")) for g in groups]

    if len(groups) <= 7 and len(checks) <= 7:
        best, best_cost = None, None
        for perm in permutations(range(len(checks)), n):
            cost = sum(abs(totals[i] - checks[j][1]) for i, j in enumerate(perm))
            if best_cost is None or cost < best_cost:
                best, best_cost = perm, cost
        return [(groups[i], checks[j]) for i, j in enumerate(best or ())]

    taken: set[int] = set()
    pairs = []
    for i in sorted(range(len(groups)), key=lambda i: -totals[i]):
        cand = min(
            (j for j in range(len(checks)) if j not in taken),
            key=lambda j: abs(totals[i] - checks[j][1]), default=None,
        )
        if cand is None:
            break
        taken.add(cand)
        pairs.append((groups[i], checks[cand]))
    return pairs


def reconcile_batch(
    groups: Sequence[LineGroup],
    wf_checks: Sequence[tuple[str, Decimal]],
    return_item_checks: Sequence[str] = (),
    tolerance: Decimal = DEFAULT_TOLERANCE,
    resolve_residuals: bool = False,
) -> BatchReconciliation:
    """Reconcile one batch's coded lines against the WF check register.

    The register is the authority; section D never enters this function. Resolution
    runs in three passes, weakest assumption last:

    1. Solve each form group against the still-unclaimed checks. A group covering
       several checks (batch 632's four-check form at 115,589.65) resolves here as
       a ``split``.
    2. Repair single-digit misreads in the groups that failed, but only against
       checks still unclaimed after pass 1, so a repair can never steal a check
       another form legitimately owns.
    3. Whatever remains is reported, not forced. An unclaimed check is a return
       item or an unread form; an unresolved group is a break carrying the
       distance to its nearest unclaimed check, which is the analyst's next step.
    """
    pool = [(str(n), abs(Decimal(a))) for n, a in wf_checks]
    returns = {str(c).strip() for c in return_item_checks}
    claimed: set[str] = set()
    outcomes: list[CheckOutcome] = []
    group_results: list[GroupOutcome] = []
    pending: list[LineGroup] = []
    undecidable: list[str] = []

    def unclaimed() -> list[tuple[str, Decimal]]:
        return [(n, a) for n, a in pool if n not in claimed]

    # -- pass 1: solve each group against the register --
    for grp in groups:
        if not grp.amounts:
            continue
        res = partition_lines_to_checks(grp.amounts, unclaimed(), tolerance)
        if res.undecidable:
            # The lines tie either way. Guessing would produce a JE that balances
            # with the wrong money against the wrong checks — worse than an open item.
            undecidable.append(grp.key)
            group_results.append(GroupOutcome(
                key=grp.key, resolved=False,
                note=f"{len(res.segments)} vs {len(res.alternate)} segment split both "
                     f"tie; arithmetic cannot choose — needs the form image",
            ))
            continue
        if res.ok:
            status = TieStatus.SPLIT if len(res.segments) > 1 else TieStatus.TIE
            for seg in res.segments:
                claimed.add(seg.check_number)
                outcomes.append(CheckOutcome(
                    check_number=seg.check_number,
                    check_amount=seg.check_amount,
                    coded_total=seg.check_amount,
                    tie_status=status,
                    group_key=grp.key,
                    note=(f"form covers {len(res.segments)} checks"
                          if status is TieStatus.SPLIT else ""),
                ))
            group_results.append(GroupOutcome(
                key=grp.key, resolved=True,
                checks=[s.check_number for s in res.segments],
            ))
        else:
            pending.append(grp)

    # -- pass 2: single-digit repair, against what pass 1 left behind --
    still_pending: list[LineGroup] = []
    for grp in pending:
        repaired = list(grp.amounts)
        fixes: list[str] = []
        fixed_checks: set[str] = set()
        for i, amt in enumerate(repaired):
            hit = find_digit_correction(amt, unclaimed(), tolerance)
            if hit is None:
                continue
            # Provisional: only kept if the whole group then solves.
            repaired[i] = hit[1]
            fixed_checks.add(hit[0])
            fixes.append(f"{amt} -> {hit[1]} (chk {hit[0]})")
        if not fixes:
            still_pending.append(grp)
            continue
        res = partition_lines_to_checks(repaired, unclaimed(), tolerance)
        if not res.ok:
            still_pending.append(grp)
            continue
        multi = len(res.segments) > 1
        for seg in res.segments:
            claimed.add(seg.check_number)
            # Only the check whose amount was actually edited carries the repair. The
            # rest of the group tied on their own and must not inherit the flag.
            repaired_here = [f for f in fixes if f.endswith(f"(chk {seg.check_number})")]
            outcomes.append(CheckOutcome(
                check_number=seg.check_number,
                check_amount=seg.check_amount,
                coded_total=seg.check_amount,
                tie_status=(
                    TieStatus.CORRECTED if repaired_here
                    else TieStatus.SPLIT if multi
                    else TieStatus.TIE
                ),
                group_key=grp.key,
                note="; ".join(repaired_here),
            ))
        group_results.append(GroupOutcome(
            key=grp.key, resolved=True,
            checks=[s.check_number for s in res.segments],
            note="; ".join(fixes),
        ))

    # -- pass 3 (opt-in): force the remainder to balance against the register --
    #
    # Nothing here is a *reading* of the document — the coded total failed to verify,
    # so the register's figure is substituted and the item is flagged. Two shapes:
    #
    #   one code block -> SNAP the amount to the check. The coding is unambiguous,
    #                     only the digits were wrong (648: 145,248.16 -> 145,245.15).
    #   many code blocks -> keep the coded lines, since we cannot tell WHICH row is
    #                     wrong, and book the difference as a PLUG (650 p3's 23-row
    #                     grid, 21,625.00 over the register).
    #
    # Every outcome carries needs_review=True. This makes a journal post, not a
    # correct one; an analyst still has to look at the page.
    if resolve_residuals and still_pending:
        for grp, (num, amt) in _best_assignment(still_pending, unclaimed()):
            total = sum(grp.amounts, Decimal("0"))
            gap = amt - total
            claimed.add(num)
            single_block = len(grp.amounts) == 1
            outcomes.append(CheckOutcome(
                check_number=num, check_amount=amt,
                coded_total=amt if single_block else total,
                plug=Decimal("0") if single_block else gap,
                tie_status=TieStatus.SNAPPED if single_block else TieStatus.PLUG,
                group_key=grp.key, variance=gap, needs_review=True,
                note=(f"coded {total} did not verify; amount taken from the register "
                      f"({gap:+} adjustment)" if single_block else
                      f"coded {total} across {len(grp.amounts)} rows did not verify; "
                      f"{gap:+} booked as a plug — the wrong row is unidentified"),
            ))
            group_results.append(GroupOutcome(
                key=grp.key, resolved=True, checks=[num],
                note=f"forced to chk {num} ({gap:+})",
            ))
        still_pending = [
            g for g in still_pending
            if not any(gr.key == g.key and gr.resolved for gr in group_results)
        ]

    # -- report whatever is left --
    for grp in still_pending:
        total = sum(grp.amounts, Decimal("0"))
        nearest = min(
            unclaimed(), key=lambda c: abs(c[1] - total), default=None,
        )
        note = ""
        if nearest is not None:
            note = (f"group totals {total}; nearest unclaimed check {nearest[0]} "
                    f"at {nearest[1]} (short by {nearest[1] - total})")
        group_results.append(GroupOutcome(
            key=grp.key, resolved=False, unresolved_total=total, note=note,
        ))

    for num, amt in unclaimed():
        outcomes.append(CheckOutcome(
            check_number=num, check_amount=amt, coded_total=Decimal("0"),
            tie_status=TieStatus.RETURN_ITEM if num in returns else TieStatus.NO_FORM,
            variance=-amt,
            note="bounced, never coded" if num in returns
                 else "no coded lines found for this check",
        ))

    # Carry each form's unreadable-amount count onto its outcome, and mark any check
    # fed by such a form for review even when it ties perfectly.
    unpriced_by_key = {g.key: g.unpriced_lines for g in groups if g.unpriced_lines}
    for gr in group_results:
        gr.unpriced_lines = unpriced_by_key.get(gr.key, 0)
    for out in outcomes:
        if unpriced_by_key.get(out.group_key):
            count = unpriced_by_key[out.group_key]
            out.needs_review = True
            out.note = "; ".join(filter(None, (
                out.note,
                f"{count} coded row(s) booked at zero — distribution incomplete",
            )))

    return BatchReconciliation(
        outcomes=outcomes,
        groups=group_results,
        coded_total=sum((sum(g.amounts, Decimal("0")) for g in groups), Decimal("0")),
        wf_total=sum((a for _n, a in pool), Decimal("0")),
    )