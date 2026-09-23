"""GOCOLL code-block classifier - Engine 2.

GOCOLL is fully GO-form driven: whatever the GO-Collection-Form transcription
yields for a transaction is posted verbatim. This module substitutes **no**
fallback code block (there is no Misc / Suspense / Unclaimed / Deriva default
and no balancing plug):

- a present dimension is posted as transcribed (even if it is invalid - the
  ``validation_tab`` check flags codes that are not in the Validation Tab);
- a **missing** dimension is left blank on the eFIS line and flagged;
- a check with **no GO form** is posted with an all-blank code block and
  labelled ``no_go_form`` so it is flagged and reviewed.

Nothing is ever excluded and no code is ever invented - gaps surface as flags.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from src.models.gocoll_models import GoCollCodeBlock, GoCollTransaction

logger = logging.getLogger(__name__)

# Mandatory eFIS dimensions; a blank in any of these is flagged (still posts).
_MANDATORY: tuple[tuple[str, str], ...] = (
    ("business_unit", "BusinessUnit"),
    ("account", "Account"),
    ("resource_type", "ResourceType"),
)

_ALL_DIMS: tuple[str, ...] = (
    "business_unit", "account", "resource_type", "operating_unit", "resp_center",
    "project", "activity_id", "process", "location", "product", "affiliate",
    "alloc_pool",
)

@dataclass
class ClassifiedLine:
    """A transaction carried through with its transcribed code block."""
    transaction: GoCollTransaction
    code_block: GoCollCodeBlock
    classification: str  # "go_form" | "no_go_form"
    line_descr: str
    warnings: list[str]

def _has_any_code(cb: GoCollCodeBlock) -> bool:
    return any((getattr(cb, attr) or "").strip() for attr in _ALL_DIMS)

def classify_transaction(txn: GoCollTransaction) -> ClassifiedLine:
    """Pass the transcribed GO-form block through verbatim.

    No fallback substitution. A blank mandatory dimension is recorded as a
    warning (surfaced in JE Flags); an empty block is labelled ``no_go_form``.
    """
    warnings: list[str] = list(txn.warnings)
    descr = txn.line_descr or ""
    if not descr:
        # Prefer check number as reference; fall back to batch number only.
        descr = (str(txn.check_number) if txn.check_number else f"Batch {txn.batch_number}")[:30]
    cb = txn.code_block

    if not _has_any_code(cb):
        warnings.append("no GO form matched - line posted with a blank code block")
        classification = "no_go_form"
    else:
        classification = "go_form"
        missing = [
            label for attr, label in _MANDATORY
            if not (getattr(cb, attr) or "").strip()
        ]
        if missing:
            warnings.append(
                "missing code dimension(s) left blank: " + ", ".join(missing)
            )

    return ClassifiedLine(
        transaction=txn,
        code_block=replace(cb),
        classification=classification,
        line_descr=descr,
        warnings=warnings,
    )

def classify_transactions(
    transactions: list[GoCollTransaction],
) -> list[ClassifiedLine]:
    """Classify every transaction in a batch (pure pass-through + flags)."""
    classified = [classify_transaction(t) for t in transactions]
    no_form = sum(1 for c in classified if c.classification == "no_go_form")
    blanks = sum(
        1 for c in classified
        if c.classification == "go_form"
        and any("missing code dimension" in w for w in c.warnings)
    )
    if no_form or blanks:
        logger.info(
            "GOCOLL classify: %d line(s), %d without a GO form, %d with blank "
            "mandatory dimension(s)",
            len(classified), no_form, blanks,
        )
    return classified