"""GOCOLL JE - Explainability Report Renderer.

Transforms the ``GoCollPipelineResult`` into a structured, auditor-ready
report consumed by the React UI Explainability tab (``data.explainability_report``).

The output matches the shared ``BusinessExplainability`` shell contract:
a ``sections`` list where each section carries ``id``, ``order``, ``heading``,
``description``, ``purpose``, ``narrative``, ``interpretation``,
``how_agent_uses``, ``citations`` and a pipeline-specific ``content`` dict.

Static report metadata and section language come from ``agent/config/
explainability.yaml``. Narratives are deterministic and populated from the
current pipeline result.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from src.models.enums import (
    GATE_CHECK_NAMES,
    CheckName,
    ValidationStatus,
    check_label,
)
from src.utils import to_float as _to_float
from src.validation.gocoll_validation_tab_check import split_findings_by_line_kind

logger = logging.getLogger(__name__)

_SERVICE_ID = "ms-duke-je-gocoll-agent-service"
_CONFIG_NAME = "explainability.yaml"


def _load_local_config() -> dict[str, Any]:
    config_path = Path(__file__).resolve().parents[1] / "config" / _CONFIG_NAME
    with config_path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# Registry config is loaded once per process into this module-level holder.
# async initialisation sets this; sync fallback reads local YAML.
_registry_config: dict[str, Any] | None = None


async def load_explainability_from_registry() -> None:
    """Load explainability config from the central registry."""
    global _registry_config
    if _registry_config is not None:
        return
    from ms_duke_je_common.config_loader import ConfigLoader

    loader = ConfigLoader(service_id=_SERVICE_ID)
    _registry_config = await loader.load(_CONFIG_NAME)


@functools.lru_cache(maxsize=1)
def _load_config_cached() -> dict[str, Any]:
    """Load config once per process; safe to call from any thread."""
    if _registry_config is not None:
        return _registry_config
    try:
        try:
            asyncio.get_running_loop()
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(1) as pool:
                pool.submit(asyncio.run, load_explainability_from_registry()).result(timeout=10)
        except RuntimeError:
            asyncio.run(load_explainability_from_registry())
    except Exception:
        logger.warning(
            "GOCOLL explainability registry load failed; using bundled YAML",
            exc_info=True,
        )
    return _registry_config or _load_local_config()


def _load_config() -> dict[str, Any]:
    return _load_config_cached()


def _section_config(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(sec.get("id")): sec
        for sec in cfg.get("sections", [])
        if isinstance(sec, dict) and sec.get("id")
    }


def _apply_section_config(
    sections: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    by_id = _section_config(cfg)
    configured: list[dict[str, Any]] = []
    for section in sections:
        sec_cfg = by_id.get(str(section.get("id")), {})
        if sec_cfg.get("enabled", True) is False:
            continue
        merged = dict(section)
        for key in (
            "order",
            "heading",
            "description",
            "purpose",
            "interpretation",
            "how_agent_uses",
            "citations",
            "enabled",
        ):
            if key in sec_cfg:
                merged[key] = sec_cfg[key]
        configured.append(merged)
    return sorted(configured, key=lambda s: int(s.get("order", 99)))


def _fmt_currency(value: Any, formatting: dict[str, Any] | None = None) -> str:
    from src.utils import fmt_currency
    return fmt_currency(value, formatting)


def _fmt_int(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-"


def build_gocoll_explainability(
    pipeline: Any,
    *,
    run_id: str = "",
    period_label: str = "",
    journal_date: str = "",
    agent_version: str = "1.0.0",
) -> dict[str, Any]:
    """Render the GOCOLL explainability report.

    Args:
        pipeline: ``GoCollPipelineResult``.
        run_id: Pipeline run identifier.
        period_label: eFIS header label.
        journal_date: eFIS journal date.
        agent_version: Version string for the footer.

    Returns:
        Report dict with ordered sections for the UI Explainability tab.
    """
    cfg = _load_config()
    report_cfg = cfg.get("report", {})
    fmt_cfg = cfg.get("formatting", {})
    def fmt_currency(value: Any) -> str:
        return _fmt_currency(value, fmt_cfg)

    extraction = getattr(pipeline, "extraction", None)
    assembly = getattr(pipeline, "assembly", None)
    processing = getattr(pipeline, "processing_result", None)
    reconciliation = getattr(pipeline, "reconciliation", None)
    wf_report = getattr(pipeline, "wf_report", None)
    treasury_report = getattr(pipeline, "treasury_report", None)
    validation_master = getattr(pipeline, "validation_master", None)
    je = getattr(assembly, "journal_entry", None) if assembly else None

    batches = list(getattr(extraction, "batches", []) or []) if extraction else []
    all_lines = list(getattr(je, "all_lines", []) or []) if je else []

    period_label = period_label or (getattr(extraction, "period_label", "") if extraction else "")
    journal_date = journal_date or (getattr(extraction, "journal_date", "") if extraction else "")

    status = getattr(processing, "status", "UNKNOWN") if processing else "UNKNOWN"
    status_str = status.value if hasattr(status, "value") else str(status)
    is_verified = status == ValidationStatus.PASS or status_str in ("VERIFIED", "PASS")

    txn_count = getattr(extraction, "transaction_count", 0) if extraction else 0
    line_count = getattr(je, "line_count", 0) if je else 0
    monetary_total = _to_float(getattr(je, "monetary_total", 0)) if je else 0.0
    is_balanced = bool(getattr(assembly, "is_balanced", False)) if assembly else False
    grand_check_total = sum(_to_float(getattr(b, "check_total", 0)) for b in batches)
    batch_numbers = ", ".join(getattr(b, "batch_number", "") for b in batches) or "-"

    total_debits = sum(
        v
        for v in (_to_float(getattr(ln, "monetary_amount", 0)) for ln in all_lines)
        if v > 0
    )
    otc_total = _to_float(
        getattr(reconciliation, "treasury_otc_total", 0) if reconciliation else 0
    )
    # Deposited checks plus any OTC deposit should equal what the journal books.
    # A residual is deposit money that did not reach the journal (e.g. a control
    # amount that reconciliation satisfied from the printed transaction total),
    # so it has to be stated rather than left for the reviewer to discover.
    unjournalized = round(grand_check_total + otc_total - total_debits, 2)

    checks = list(getattr(processing, "checks", []) or []) if processing else []

    def _status_of(check: Any) -> str:
        st = getattr(check, "status", "")
        return st.value if hasattr(st, "value") else str(st)

    check_statuses = [_status_of(c) for c in checks]
    passed = sum(1 for s in check_statuses if s == ValidationStatus.PASS)
    needs_review = sum(1 for s in check_statuses if s == ValidationStatus.REVIEW)
    blocked = sum(
        1 for s in check_statuses
        if s in (ValidationStatus.FAIL, ValidationStatus.FAILED)
    )

    result_label = (
        "Verified - Ready for Review" if is_verified
        else "Exceptions Found - Review Required"
    )

    sections: list[dict[str, Any]] = []

    # — Section 1: Executive Summary
    sections.append({
        "id": "executive_summary",
        "order": 1,
        "heading": "Executive Summary",
        "description": "Top-line outcome of this GOCOLL lockbox journal entry.",
        "purpose": (
            "Gives the reviewer the headline result, totals, and balance status "
            "before drilling into the per-engine detail below."
        ),
        "narrative": (
            f"For journal date {journal_date}, the GO Collections journal "
            if journal_date else
            "No journal date was resolved for this run, so the eFIS upload "
            "carries a blank Journal Date and must be dated before posting."
            "The GO Collections journal "
        )
        + f"({period_label or 'period label not resolved'}) was prepared from "
        f"{len(batches)} lockbox deposit batch(es) ({batch_numbers}). "
        f"The pipeline extracted {txn_count} transaction(s) and assembled "
        f"{line_count} eFIS line(s) posting {fmt_currency(total_debits)} of "
        f"debits against the same value in credits, so the journal nets to "
        f"{fmt_currency(monetary_total)} as eFIS requires. "
        f"Deposited checks across all batches totalled "
        f"{fmt_currency(grand_check_total)}"
        + (
            f", plus {fmt_currency(otc_total)} of Treasury over-the-counter "
            f"deposits"
            if otc_total else ""
        )
        + ". "
        + (
            f"{fmt_currency(unjournalized)} of that deposited money is not in "
            f"the journal and must be explained before posting. "
            if abs(unjournalized) >= 0.01 else
            "Deposited money and journalized debits agree. "
        )
        + f"The overall result is \"{result_label}\" (pipeline status "
        f"{status_str}). "
        + (
            f"{needs_review} check(s) need reviewer follow-up before "
            f"posting; see the validation section. "
            if needs_review else
            "No reviewer follow-up items were raised. "
        )
        if is_verified else
        "The reviewer should focus on the validation findings below "
        "before approving the entry."
    ),
    "interpretation": (
        "A balanced ($0.00) journal with all batches reconciled to the bank "
        "deposit indicates the entry is ready to post."
    ),
    "how_agent_uses": (
        "The agent derives these totals directly from the assembled journal "
        "and the per-batch deposit controls - no figures are estimated."
    ),
    "citations": [Path(getattr(b, "source_pdf", "") or "").name for b in batches if getattr(b, "source_pdf", "")],
    "content": {
        "Run ID": run_id or "-",
        "Period": period_label or "Not resolved",
        "Journal Date": journal_date or "Not resolved - must be set before posting",
        "Batches": batch_numbers,
        "Transactions": txn_count,
        "eFIS Lines": line_count,
        "Total Debits": fmt_currency(total_debits),
        "Total Credits": fmt_currency(-total_debits),
        "Nets To": fmt_currency(monetary_total),
        "Balanced": "Yes" if is_balanced else "No",
        # Bridge from bank deposits to journalized debits so the reviewer
        # never has to subtract three separately reported totals by hand.
        "Deposited Check Total": fmt_currency(grand_check_total),
        "Treasury OTC Deposits": fmt_currency(otc_total),
        "Not Journalized": fmt_currency(unjournalized),
        "Checks OK": passed,
        "Checks Needing Review": needs_review,
        "Checks Blocking": blocked,
        "pipeline_status": status_str,
        "Overall Result": result_label,
    },
    })

    # — Section 2: Source Documents
    file_inventory = []
    for b in batches:
        src = getattr(b, "source_pdf", "") or ""
        file_inventory.append({
            "filename": Path(src).name if src else f"Batch {getattr(b, 'batch_number', '?')}",
            "type": "Lockbox batch PDF",
            "batch_number": getattr(b, "batch_number", ""),
            "transactions": getattr(b, "transaction_count", 0),
            "check_total": fmt_currency(getattr(b, "check_total", 0)),
            "processing_method": "GPT vision (LLM-only) over rendered page images",
        })
    # The control workbooks are consumed inputs too; omitting them understated
    # files_processed and disagreed with the calculation-logic data_sources list.
    for label, report_obj, role in (
        ("Validation Tab workbook", validation_master, "PeopleSoft dimension master - GL code validation"),
        ("WF Transactions Report", wf_report, "Wells Fargo gross check totals - bank control B"),
        ("Treasury Report", treasury_report, "BAI 115 lockbox deposits and BAI 566 return items - bank control C"),
    ):
        source_file = getattr(report_obj, "source_file", "") if report_obj else ""
        file_inventory.append({
            "filename": Path(str(source_file)).name if source_file else label,
            "type": "Control workbook",
            "role": role,
            "provided": report_obj is not None,
            "processing_method": "Deterministic workbook parse (openpyxl)",
        })
    sections.append({
        "id": "source_documents",
        "order": 2,
        "heading": "Source Documents",
        "description": "Inventory of the lockbox batch PDFs consumed in this run.",
        "purpose": "Documents the complete audit trail from source PDFs to results.",
        "narrative": (
            f"The pipeline processed {len(batches)} lockbox batch PDF(s): "
            f"{batch_numbers}. Each batch is split into per-check transactions; "
            f"every page is read with GPT vision - the GO-Collection-Form code "
            f"blocks and the check-identifying fields are transcribed from the "
            f"page images (the PDF text layer is not used for distributions)."
        ),
        "interpretation": (
            "Confirm all expected batch PDFs were included and that the "
            "per-batch check totals match the lockbox deposit advices."
        ),
        "how_agent_uses": (
            "Files are classified as batch PDFs vs GO forms by content/filename, "
            "then routed to the extraction coordinator."
        ),
        "citations": [f["filename"] for f in file_inventory],
        "content": {
            "file_inventory": file_inventory,
            "files_processed": len(file_inventory),
            "batch_pdfs": len(batches),
        },
    })

    # — Section 3: Extraction
    tier_counts: dict[str, int] = {}
    for b in batches:
        for t in getattr(b, "transactions", []) or []:
            tier = getattr(t, "extraction_tier", "") or "unknown"
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
    review_items = []
    if extraction is not None:
        for t in getattr(extraction, "review_items", []) or []:
            review_items.append({
                "batch_number": getattr(t, "batch_number", ""),
                "check_number": getattr(t, "check_number", ""),
                "amount": fmt_currency(getattr(t, "amount", 0)),
                "reason": getattr(t, "review_reason", ""),
            })
    sections.append({
        "id": "extraction",
        "order": 3,
        "heading": "Reading the Lockbox Deposits",
        "description": "How raw batch PDFs became per-transaction units.",
        "purpose": "Shows extraction coverage and any items flagged for review.",
        "narrative": (
            f"{txn_count} transaction(s) were extracted across {len(batches)} "
            f"batch(es). Extraction tiers used: "
            + ", ".join(f"{k} ({v})" for k, v in tier_counts.items() if k) + ". "
            + (
                f"{len(review_items)} item(s) were flagged for human review "
                f"because the coded distribution could not be reconciled to the "
                f"printed check total."
                if review_items else
                "No transactions required human review."
            )
        ),
        "interpretation": (
            "Vision-tier reads on dense/low-quality forms warrant a spot check. "
            "Any review items must be cleared before posting."
        ),
        "how_agent_uses": (
            "Every page is read with GPT vision (LLM-only); the PDF text layer is "
            "not used for distributions. Coded distributions that cannot be "
            "reconciled to the deposited check total are routed to human review."
        ),
        "citations": [],
        "content": {
            "transaction_count": txn_count,
            "extraction_tiers": tier_counts,
            "review_items": review_items,
        },
    })

    # — Section 4: Classification
    class_counts: dict[str, int] = {}
    for ln in all_lines:
        cls = getattr(ln, "classification", "") or getattr(ln, "line_kind", "") or "unknown"
        class_counts[cls] = class_counts.get(cls, 0) + 1
    sections.append({
        "id": "classification",
        "order": 4,
        "heading": "Identifying the GL Coding",
        "description": "How each coded distribution was bucketed for posting.",
        "purpose": "Explains which GL treatment each transaction received.",
        "narrative": (
            "Each transcribed GO-Collection-Form code block was classified into "
            "a posting bucket. Distribution across buckets: "
            + ", ".join(f"{k} ({v})" for k, v in sorted(class_counts.items())) + "."
        ),
        "interpretation": (
            "A high share of 'go_form' coded lines indicates clean forms. "
            "Lines labelled 'no_go_form' need a code block from the analyst "
            "(Suspense >$5,000, Misc otherwise, or code provided by MARBS)."
        ),
        "how_agent_uses": (
            "The code-block classifier posts each line verbatim from the GO form "
            "(go_form). Lines with no readable form are labelled no_go_form and "
            "left blank for analyst review - no fallback code block is substituted."
        ),
        "citations": [],
        "content": {
            "class_counts": class_counts,
        },
    })

    # — Section 5: JE Assembly
    batch_entries = []
    for be in (getattr(je, "batch_entries", []) or []) if je else []:
        cash = getattr(be, "cash_line", None)
        batch_entries.append({
            "batch_number": getattr(be, "batch_number", ""),
            "cash_amount": fmt_currency(getattr(cash, "monetary_amount", 0)) if cash else "-",
            "detail_count": len(getattr(be, "detail_lines", []) or []),
            "detail_total": fmt_currency(getattr(be, "detail_total", 0)),
            "batch_total": fmt_currency(getattr(be, "batch_total", 0)),
            "balanced": "Yes" if _to_float(getattr(be, "batch_total", 0)) == 0.0 else "No",
        })
    sections.append({
        "id": "assembly",
        "order": 5,
        "heading": "Building the Balanced Journal",
        "description": "How the balanced eFIS journal was built per batch.",
        "purpose": "Shows the cash + detail structure and the zero-balance proof.",
        "narrative": (
            f"The journal assembled {line_count} eFIS line(s) across "
            f"{len(batch_entries)} batch(es). Each batch posts one cash/control "
            f"line equal to the negative sum of its coded detail lines, so the "
            f"MonetaryAmount column sums to {fmt_currency(monetary_total)}."
        ),
        "interpretation": (
            "Every batch total must be $0.00. The header description carries the "
            "lockbox and batch range for traceability. Note that the cash line is "
            "written into the workbook as a live formula, so a $0.00 net proves "
            "the workbook recomputes - not that the coded amounts were verified. "
            "The three control cells in row 2 are the figures to tie against."
        ),
        "how_agent_uses": (
            "Detail lines are forced negative and the cash/control line is "
            "computed as SUM(details), so each batch nets to zero. No balancing "
            "plug is fabricated; a coded total that differs from the deposited "
            "check is surfaced as a reconciliation flag for analyst review."
        ),
        "citations": [],
        "content": {
            "header_desc": getattr(je, "header_desc", "") if je else "",
            "journal_date": getattr(je, "journal_date", "") if je else "",
            "journal_mask": getattr(je, "journal_mask", "") if je else "",
            "line_count": line_count,
            "monetary_total": fmt_currency(monetary_total),
            "is_balanced": "Yes" if is_balanced else "No",
            # The same three control cells the output workbook writes into row 2
            # of Sheet1, so the reviewer can tie this section to the file by eye.
            "control_cells": [
                {
                    "cell": "A2",
                    "measures": "Line count",
                    "value": str(len(all_lines)),
                },
                {
                    "cell": "B2",
                    "measures": "Gross movement (absolute)",
                    "value": fmt_currency(
                        sum(
                            abs(_to_float(getattr(ln, "monetary_amount", 0)))
                            for ln in all_lines
                        )
                    ),
                },
                {
                    "cell": "C2",
                    "measures": "Net total (must be zero)",
                    "value": fmt_currency(monetary_total),
                },
            ],
            "batch_entries": batch_entries,
        },
    })

    # — Section 6: Reconciliation
    recon_batches = list(getattr(reconciliation, "batches", []) or []) if reconciliation else []
    recon_rows = []
    for rb in recon_batches:
        recon_rows.append({
            "batch_number": getattr(rb, "batch_number", ""),
            "coded_amount": fmt_currency(getattr(rb, "coded_amount", None)),
            "wf_amount": fmt_currency(getattr(rb, "wf_amount", None)),
            "treasury_amount": fmt_currency(getattr(rb, "treasury_amount", None)),
            "return_items": fmt_currency(getattr(rb, "return_items", 0)),
            "status": getattr(rb, "status", ""),
            "detail": getattr(rb, "detail", ""),
        })
    flagged = list(getattr(reconciliation, "flagged", []) or []) if reconciliation else []
    return_item_batches = (
        list(getattr(reconciliation, "return_item_batches", []) or [])
        if reconciliation else []
    )

    sections.append({
        "id": "reconciliation",
        "order": 6,
        "heading": "Tying the Journal to the Bank",
        "description": "A/B/C tie-out between coded JE amounts, WF totals, and Treasury bank activity.",
        "purpose": "Shows whether each batch ties to independent bank controls.",
        "narrative": (
            f"{len(recon_batches)} batch reconciliation(s) were evaluated. "
            f"{len(return_item_batches)} batch(es) had A-vs-B gaps explained by "
            f"Treasury return items, and {len(flagged)} batch(es) require analyst "
            f"review for missing controls or unexplained differences."
            if recon_batches else
            "No reconciliation rows were available for this run."
        ),
        "interpretation": (
            "A tie or return-item explanation is acceptable. Missing WF controls "
            "or unexplained differences require review before approval."
        ),
        "how_agent_uses": (
            "For each batch, A = coded JE amount, B = WF gross check total, and "
            "C = Treasury BAI 115 lockbox deposit. BAI 566 return items can "
            "explain A-vs-B gaps. In addition, each check's coded total is "
            "matched by amount to the WF report's per-check amounts so a single "
            "misread is localised rather than masked by the batch total. A "
            "nonzero Treasury OTC total creates balanced cash/detail lines and "
            "a review flag because the negative detail accounting must be "
            "confirmed before posting."
        ),
        "citations": [],
        "content": {
            "batch_count": len(recon_batches),
            "flagged_count": len(flagged),
            "return_item_batch_count": len(return_item_batches),
            "treasury_return_items_total": fmt_currency(
                getattr(reconciliation, "treasury_return_items_total", 0)
                if reconciliation else 0
            ),
            "treasury_otc_total": fmt_currency(
                getattr(reconciliation, "treasury_otc_total", 0)
                if reconciliation else 0
            ),
            "batches": recon_rows,
        },
    })

    # — Section 7: Validation
    check_rows = []
    cash_line_rejections = 0
    for c in checks:
        st = getattr(c, "status", "")
        st_str = st.value if hasattr(st, "value") else str(st)
        name = str(getattr(c, "check_name", "") or "-")
        row = {
            "check_name": name,
            "label": check_label(name),
            "status": st_str,
            "severity": "blocking" if name in GATE_CHECK_NAMES else "advisory",
            "details": getattr(c, "details", ""),
        }
        if name == CheckName.VALIDATION_TAB:
            on_detail, on_cash = split_findings_by_line_kind(
                list(getattr(c, "failures", []) or [])
            )
            cash_line_rejections = len(on_cash)
            row["coded_detail_findings"] = len(on_detail)
            row["generated_cash_line_findings"] = cash_line_rejections
        check_rows.append(row)

    sections.append({
        "id": "validation",
        "order": 7,
        "heading": "Pre-Posting Review Checks",
        "description": "Automated rule-pack checks gating the journal.",
        "purpose": "Confirms the entry is balanced, reconciled, and complete.",
        "narrative": (
            f"{len(checks)} check(s) ran: {passed} OK, {needs_review} needing "
            f"reviewer follow-up, {blocked} blocking. The overall pipeline status "
            f"is {status_str}. "
            + (
                "Both posting-blocking checks passed, so the journal is "
                "postable. "
                + (
                    f"The {needs_review} advisory finding(s) below must be "
                    f"cleared by the reviewer before posting."
                    if needs_review else
                    "No advisory findings were raised."
                )
            )
            if is_verified else
            "A posting-blocking check failed, so the journal must not be "
            "posted until it is resolved."
        ),
        # Run-conditional text has to live in the narrative: the YAML
        # overrides interpretation, so anything appended there is discarded.
        + (
            f" Note that {cash_line_rejections} of the Validation Tab "
            f"rejection(s) are on the cash/control line the agent generates "
            f"itself, meaning the Validation Tab master does not carry that "
            f"code block. Those are a master-data coverage gap rather than "
            f"coded detail to re-key, and are counted separately below."
            if cash_line_rejections else ""
        ),
        "interpretation": (
            "Only the balance and per-batch control-total checks prevent posting. "
            "The coding-quality checks are advisory by design, because GO "
            "Collections coding is form-driven and needs a human decision rather "
            "than an automatic rejection - but every advisory finding must still "
            "be cleared before the entry is posted."
        ),
        "how_agent_uses": (
            "The GOCOLL rule pack runs balance and per-batch control totals as "
            "hard gates, then code-block completeness, Validation Tab code "
            "validation, PeopleSoft Derivation follow-up for values the "
            "Validation Tab rejected, and the A/B/C bank reconciliation as "
            "advisory checks."
        ),
        "citations": [],
        "content": {
            "overall_status": status_str,
            "total_checks": len(checks),
            "ok": passed,
            "needs_review": needs_review,
            "blocking": blocked,
            "blocking_checks": sorted(str(n) for n in GATE_CHECK_NAMES),
            "checks": check_rows,
        },
    })

    # — Section 8: Next Steps (SOP)
    # The agent's deliverable is the eFIS workbook; the SOP continues past it.
    # Naming the remaining steps (and who to contact) keeps the reviewer from
    # having to hold the procedure in their head.
    status_by_check = {r["check_name"]: r["status"] for r in check_rows}
    next_steps: list[dict[str, str]] = []

    if not journal_date:
        next_steps.append({
            "action": "Set the Journal Date",
            "detail": (
                "The upload carries a blank Journal Date. Enter the month-end "
                "date for the period being recorded before submitting to eFIS."
            ),
            "owner": "Preparer",
        })

    if class_counts.get("no_go_form"):
        next_steps.append({
            "action": "Resolve checks with no GO Collection Form",
            "detail": (
                f"{class_counts['no_go_form']} line(s) posted with blank "
                "dimensions. Contact the MARBS team (MISCAR@duke-energy.com) to "
                "confirm whether the payment is theirs or whether they know the "
                "accounting code block. If they say to exclude it, remove the "
                "line, add the email to the Support tab with the amount and "
                "batch number, and note \"See attached Support\" in the Control "
                "vs WF Report tab. If they supply a code block, use it and file "
                "the email. If they do not know, use Suspense when the amount is "
                "over $5,000, otherwise Miscellaneous."
            ),
            "owner": "Preparer + MARBS",
        })

    if status_by_check.get("validation_tab") == ValidationStatus.REVIEW or \
       status_by_check.get("deriva_guard") == ValidationStatus.REVIEW:
        next_steps.append({
            "action": "Confirm the values the Validation Tab rejected",
            "detail": (
                "For each rejected value, confirm the accounting code block is "
                "valid using the PeopleSoft Derivation validation setup. If it "
                "is valid, add only that value to the bottom of its column on "
                "the Validation tab so it joins the dropdown. If it is not "
                "valid, email the submitting user with screenshots of the check, "
                "the GO form and the error, and use Suspense (over $5,000) or "
                "Miscellaneous."
            ),
            "owner": "Preparer + submitting user",
        })

    if otc_total:
        next_steps.append({
            "action": "Confirm the over-the-counter deposit code block",
            "detail": (
                f"{fmt_currency(otc_total)} of over-the-counter deposits was "
                "journalized with a blank offsetting code block. Confirm with "
                "Tim Coffey which accounting code block to use before posting."
            ),
            "owner": "Preparer + Tim Coffey",
        })

    if abs(unjournalized) >= 0.01:
        next_steps.append({
            "action": "Explain the deposit money that is not journalized",
            "detail": (
                f"Deposited checks plus OTC exceed journalized debits by "
                f"{fmt_currency(unjournalized)}. The bank figure is the certain "
                "one, so verify the amounts recorded before posting."
            ),
            "owner": "Preparer",
        })

    next_steps.extend([
        {
            "action": "Preview in eFIS",
            "detail": (
                "Upload the workbook with feed type \"Journal Entry - Preview\", "
                "then open the Feeder Error Report for today and view the JOURNAL "
                "feed. Severity X is a fatal error and will not load to "
                "PeopleSoft; severity W is a warning that must be corrected "
                "before posting. Fix accounting errors in the workbook - for "
                "\"not chargeable\" errors, ask the person who supplied the "
                "accounting for valid values."
            ),
            "owner": "Preparer",
        },
        {
            "action": "Submit in eFIS",
            "detail": (
                "Once the error report is clear, re-upload with feed type "
                "\"Journal Entry - Submit\" and confirm the Feeder Error Report "
                "row turns green, meaning it loaded to PeopleSoft."
            ),
            "owner": "Preparer",
        },
        {
            "action": "Edit, submit and attach in PeopleSoft",
            "detail": (
                "In the GL WorkCenter, open the journal for business unit 10900 "
                "and the period end date, run Edit Journal until the lines show "
                "status \"V\", then run Submit Journal. Add the supporting files "
                "on the Header tab, save, and append the Journal ID to the "
                "workbook filename before notifying the reviewer."
            ),
            "owner": "Preparer + Reviewer",
        },
    ])

    sections.append({
        "id": "next_steps",
        "order": 8,
        "heading": "Next Steps",
        "description": "The SOP steps that follow this workbook.",
        "purpose": (
            "The agent's output is the eFIS upload workbook. This section lists "
            "the remaining SOP actions, and who to contact, so nothing is missed "
            "between generation and posting."
        ),
        "narrative": (
            f"{len(next_steps)} step(s) remain before this journal is posted and "
            f"handed to the reviewer."
        ),
        "interpretation": (
            "Work the run-specific items first - they need someone else's input "
            "and set the pace. The eFIS and PeopleSoft steps are the standard "
            "close-out sequence."
        ),
        "how_agent_uses": (
            "These steps are taken from the GO Collections SOP. The agent does "
            "not perform them; it reports which ones this run triggered."
        ),
        "citations": ["CORP GOCOLL Weekly JE SOP"],
        "content": {
            "step_count": len(next_steps),
            "steps": next_steps,
        },
    })

    sections = _apply_section_config(sections, cfg)

    return {
        "report_title": report_cfg.get(
            "title", "GOCOLL JE - Explainability Report"
        ),
        "report_subtitle": report_cfg.get("subtitle") or period_label or "",
        "report_version": report_cfg.get("report_version", "1.0"),
        "generated_at": datetime.now().strftime(
            fmt_cfg.get("timestamp_format", "%B %d, %Y %I:%M %p")
        ),
        "agent_version": (
            agent_version if report_cfg.get("show_agent_version", True) else None
        ),
        "footer": report_cfg.get(
            "footer",
            "Generated by the GOCOLL JE agent.",
        ),
        "formatting": {
            "locale": fmt_cfg.get("locale", "en-US"),
            "currency_symbol": fmt_cfg.get("currency_symbol", "$"),
            "negative_style": fmt_cfg.get("negative_style", "parentheses"),
            "decimal_places": fmt_cfg.get("decimal_places", 2),
        },
        "sections": sections,
    }"""Placeholder for gocoll_explainability.py."""
