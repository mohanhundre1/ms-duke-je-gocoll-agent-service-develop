"""Enums for GOCOLL JE Agent — no magic strings."""

from enum import IntEnum, StrEnum


class A2AErrorCode(IntEnum):
    """JSON-RPC error codes returned to A2A callers."""

    AUTHORIZATION = -32001
    CONTENT_SAFETY = -32002
    POLICY_DENIED = -32003
    INVALID_INPUT = -32004
    INTERNAL = -32603


class ValidationStatus(StrEnum):
    """Pipeline validation gate status."""
    PASS = "PASS"
    FAIL = "FAIL"
    REVIEW = "REVIEW"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


class PipelineStatus(StrEnum):
    """Pipeline orchestration status — no magic strings."""
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    HALTED = "HALTED"


class CheckName(StrEnum):
    """Validation check identifiers — single source of truth for rule-pack references."""
    BALANCE = "balance"
    CONTROL_TOTALS = "control_totals"
    CODEBLOCK_COMPLETENESS = "codeblock_completeness"
    VALIDATION_TAB = "validation_tab"
    DERIVA_GUARD = "deriva_guard"
    RECONCILIATION = "reconciliation"
    LINE_COUNT = "line_count"
    CHECK_TIEOUT = "check_tieout"


# The only checks whose failure stops the journal from posting. Everything else
# in the GOCOLL pack is advisory: GO Collections coding is form-driven, so a
# coding finding needs a human decision rather than an automatic rejection.
# Kept here so the reporting layers cannot drift from the gate they describe.
GATE_CHECK_NAMES: frozenset[str] = frozenset({
    CheckName.BALANCE,
    CheckName.CONTROL_TOTALS,
})


# Reviewer-facing labels for check identifiers. The keys stay stable for audit
# continuity; only the label changes. DERIVA_GUARD is named for the PeopleSoft
# *Derivation* validation setup step in the SOP and has nothing to do with
# Deriva Energy, so its label says so explicitly.
CHECK_LABELS: dict[str, str] = {
    CheckName.BALANCE: "Journal balances to zero",
    CheckName.CONTROL_TOTALS: "Every batch balances to zero",
    CheckName.CODEBLOCK_COMPLETENESS: "Every line has a complete code block",
    CheckName.VALIDATION_TAB: "GL codes exist in the Validation Tab master",
    CheckName.DERIVA_GUARD: "PeopleSoft Derivation setup follow-up",
    CheckName.RECONCILIATION: "Journal ties to the bank",
    CheckName.LINE_COUNT: "Expected line count",
    CheckName.CHECK_TIEOUT: "Each check ties to the bank",
    "extraction_nonempty": "Batch PDFs produced transactions",
    "extraction_errors": "No extraction errors",
    "reconciliation_review": "Checks needing analyst review",
    "codeblock_format": "Code block values reformatted for eFIS",
}


def check_label(check_name: str) -> str:
    """Reviewer-facing label for a check identifier, falling back to the key."""
    return CHECK_LABELS.get(str(check_name), str(check_name))


class FormClass(StrEnum):
    """How a GO form's section D relates to the bank's transaction total.

    Drives whether a form's code blocks belong to one check or several. Section D
    is the preparer's declaration of what the remittance covered; the transaction
    total is what the bank actually received in that lockbox transaction.
    """

    SINGLE = "single"               # section D agrees with the bank: one check
    SPLIT = "split"                 # form covers this check plus others
    GRID_DEFECT = "grid_defect"     # coded rows disagree with section D: bad grid read
    SHORT_FORM = "short_form"       # form declares LESS than the bank received
    UNDECLARED = "undeclared"       # section D blank/unreadable: fall back to partition


class TieStatus(StrEnum):
    """Outcome of the per-check tie-out (Engine 4 `check_tieout`)."""

    TIE = "tie"                     # coded total equals the WF check amount
    SPLIT = "split"                 # resolved as part of a multi-check GO form
    CORRECTED = "corrected"         # a digit misread was repaired against the WF register
    SNAPPED = "snapped"             # amount taken from the register; coded read not trusted
    PLUG = "plug"                   # residual booked to Suspense/Misc (Phase 7)
    BREAK = "break"                 # unexplained: routed to analyst review
    RETURN_ITEM = "return_item"     # bounced check (Treasury BAI 566), never coded
    NO_FORM = "no_form"             # no GO form imaged ("Check Only" / items == 1)
    DUPLICATE = "duplicate"         # extracted check with no WF counterpart: dropped
    NO_CONTROL = "no_control"       # no WF report supplied, nothing to tie against