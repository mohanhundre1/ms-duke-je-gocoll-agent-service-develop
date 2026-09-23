"""GOCOLL JE - Calculation Logic Builder.

Builds the ``calculation_logic`` payload for the UI Calculation Logic tab: the
numbers, structure and check outcomes behind the assembled eFIS journal, stage
by stage (extraction, classification, assembly, reconciliation, validation),
using the ``engine1_extraction`` through ``engine5_validation`` keys consumed
by the GOCOLL UI renderer.

This payload is deliberately *data only*. The reviewer-facing prose that
explains what each stage means lives in two places, neither of which is here:

* the AI Explainability report (``gocoll_explainability.py``), which is rendered
  generically from payload text, and
* the per-engine React renderer, which hardcodes its own narrative - see
  ``CalculationLogicRenderer.tsx``, which reads only structural keys from this
  payload and ignores any prose it carries.

The response wrapper emits ``erp_type="gocoll"`` at the result-data level so
the renderer can dispatch explicitly without inspecting calculation details.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.models.enums import (
    GATE_CHECK_NAMES,
    CheckName,
    check_label,
)
from src.utils import to_float as _to_float

logger = logging.getLogger(__name__)

GO_FORM_FIELDS: tuple[str, ...] = (
    "business_unit",
    "account",
    "resource_type",
    "operating_unit",
    "resp_center",
    "project",
    "activity_id",
    "process",
    "location",
    "product",
    "affiliate",
    "alloc_pool",
    "amount",
    "line_descr",
)

CASH_LINE_MARKER = "(cash)"

# Human-readable classification labels (codeblock_classifier buckets).
_CLASS_LABELS: dict[str, str] = {
    "detail": "GO-Collection-Form coded distribution",
    "go_form": "GO-Collection-Form coded distribution",
    "no_go_form": "No GO form matched (blank code block, flagged)",
    "cash": "Cash control line",
    "otc_cash": "OTC cash line",
    "otc_pending": "OTC negative detail (accounting pending)",
}


def _source_name(source_file: Any, fallback: str) -> str:
    text = str(source_file or "").strip()
    return Path(text).name if text else fallback


def _status_str(obj: Any, attr: str = "status", default: str = "") -> str:
    value = getattr(obj, attr, default)
    return value.value if hasattr(value, "value") else str(value)


def build_gocoll_calculation_logic(
    pipeline: Any,
    run_id: str = "",
    period_label: str = "",
    journal_date: str = "",
) -> dict[str, Any]:
    """Build the GOCOLL calculation-logic payload from the pipeline result.

    Args:
        pipeline: ``GocollPipelineResult`` with ``.extraction``, ``.assembly``,
            ``.reconciliation`` and ``.processing_result``.
        run_id: Pipeline run identifier.
        period_label: eFIS header label, e.g. ``"JAN2025 BATCH:632-633"``.
        journal_date: eFIS journal date, e.g. ``"2026-01-31"``.

    Returns:
        UI-compatible structured dict documenting all five GOCOLL engines.
    """
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

    return {
        "run_context": _run_context(je, run_id, period_label, journal_date),
        "data_sources": _data_sources(
            batches, wf_report, treasury_report, validation_master
        ),
        "engine1_extraction": _engine1_extraction(extraction, batches),
        "engine2_classification": _engine2_classification(all_lines),
        "engine3_assembly": _engine3_assembly(je, assembly),
        "engine4_reconciliation": _engine4_reconciliation(reconciliation),
        "engine5_validation": _engine5_validation(processing),
    }


# — Run context

def _run_context(
    je: Any, run_id: str, period_label: str, journal_date: str
) -> dict[str, Any]:
    return {
        "journal_mask": getattr(je, "journal_mask", "GOCOLL") if je else "GOCOLL",
        "journal_bus_unit": getattr(je, "journal_bus_unit", "10900") if je else "10900",
        "header_desc": getattr(je, "header_desc", "") if je else period_label,
        "description": "GO Collections journal entry",
    }


# — Data sources

def _data_sources(
    batches: list[Any],
    wf_report: Any,
    treasury_report: Any,
    validation_master: Any,
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = [
        {
            "name": _source_name(
                getattr(validation_master, "source_file", ""), "Validation Tab workbook"
            ),
            "type": "PeopleSoft dimension master",
            "provides": "Valid Business Unit, Account, Resource Type, Operating Unit, "
            "Resp Center, Project, Activity, Process and Location values.",
            "used_for": "Validation - GL code checks (skipped entirely when not supplied)",
            "provided": bool(getattr(validation_master, "loaded", False)),
        },
        {
            "name": _source_name(
                getattr(wf_report, "source_file", ""), "WF Transactions Report"
            ),
            "type": "Wells Fargo lockbox transactions report",
            "provides": "Gross check amount by batch and by check - bank control B.",
            "used_for": "Reconciliation - coded amount vs WF control",
            "provided": bool(getattr(wf_report, "loaded", False)),
        },
        {
            "name": _source_name(getattr(treasury_report, "source_file", ""), "Treasury Report"),
            "type": "Treasury Total Bank Transactions report",
            "provides": "BAI 115 lockbox deposits, BAI 566 return items and "
            "over-the-counter deposit totals - bank control C.",
            "used_for": "Reconciliation - Treasury control and return-item explanations",
            "provided": bool(getattr(treasury_report, "loaded", False)),
        },
    ]
    if batches:
        sources.append(
            {
                "name": "Batch PDF",
                "type": "Lockbox batch PDF (GO Collections)",
                "batch_number": [getattr(b, "batch_number", "") for b in batches],
                "provides": "Per-check transaction summaries and the GO-Collection-Form "
                "coded GL distributions.",
                "used_for": "Extraction - per-transaction units and code blocks",
                "provided": True,
            }
        )
    return sources


# — Stage 1: Extraction

def _engine1_extraction(extraction: Any, batches: list[Any]) -> dict[str, Any]:
    return {
        "title": "Engine 1 - Extraction",
        "description": "Extracted lockbox transactions",
        "go_form_fields_extracted": {"fields": list(GO_FORM_FIELDS)},
        "batch_count": len(batches),
        "transaction_count": getattr(extraction, "transaction_count", 0) if extraction else 0,
    }


# — Stage 2: Classification

def _engine2_classification(all_lines: list[Any]) -> dict[str, Any]:
    class_counts: dict[str, int] = {}
    for ln in all_lines:
        cls = getattr(ln, "line_kind", "") or getattr(ln, "classification", "") or "unknown"
        class_counts[cls] = class_counts.get(cls, 0) + 1
    return {
        "title": "Engine 2 - Classification",
        "description": "Classified journal lines",
        "class_summary": [
            {
                "classification": cls,
                "label": _CLASS_LABELS.get(cls, cls),
                "count": cnt,
            }
            for cls, cnt in sorted(class_counts.items(), key=lambda x: (-x[1], x[0]))
        ],
    }


# — Stage 3: JE Assembly

def _engine3_assembly(je: Any, assembly: Any) -> dict[str, Any]:
    return {
        "title": "Engine 3 - JE Assembly",
        "description": "Built the balanced journal entry",
        "line_count": getattr(je, "line_count", 0) if je else 0,
        "monetary_total": _to_float(getattr(je, "monetary_total", 0)) if je else 0.0,
        "is_balanced": bool(getattr(assembly, "is_balanced", False)) if assembly else False,
    }


# — Stage 4: Reconciliation

def _engine4_reconciliation(reconciliation: Any) -> dict[str, Any]:
    recon_batches = list(getattr(reconciliation, "batches", []) or []) if reconciliation else []

    def _opt(rb: Any, attr: str) -> float | None:
        value = getattr(rb, attr, None)
        return _to_float(value) if value is not None else None

    recon_rows = []
    for rb in recon_batches:
        row = {
            "batch_number": getattr(rb, "batch_number", ""),
            "batch_total": _to_float(getattr(rb, "coded_amount", None)),
            "wf_sum": _opt(rb, "wf_amount"),
        }
        treasury_sum = _opt(rb, "treasury_amount")
        if treasury_sum is not None:
            row["treasury_sum"] = treasury_sum
        recon_rows.append(row)

    return {
        "title": "Engine 4 - Reconciliation",
        "description": "Reconciled journal totals against bank controls",
        "batch_count": len(recon_rows),
        "batches": recon_rows,
    }


# — Stage 5: Validation

def _engine5_validation(processing: Any) -> dict[str, Any]:
    checks = list(getattr(processing, "checks", []) or []) if processing else []

    check_rows: list[dict[str, Any]] = []
    for c in checks:
        status = _status_str(c)
        name = str(getattr(c, "check_name", "") or "-")
        if name not in {CheckName.BALANCE, CheckName.CONTROL_TOTALS, CheckName.VALIDATION_TAB}:
            continue

        failures = [str(f) for f in (getattr(c, "failures", []) or [])]
        details = getattr(c, "details", "")

        if name == CheckName.BALANCE:
            row: dict[str, Any] = {
                "check_name": "BALANCE_CHECK",
                "status": status,
                "details": details,
            }
        else:
            row = {
                "check_name": name,
                "label": check_label(name),
                "status": status,
                "severity": "blocking" if name in GATE_CHECK_NAMES else "advisory",
                "details": details,
                "finding_count": len(failures),
            }

        if name == CheckName.CONTROL_TOTALS:
            row["findings"] = failures
        elif name == CheckName.VALIDATION_TAB:
            on_cash = [failure for failure in failures if CASH_LINE_MARKER in failure]
            on_detail = [failure for failure in failures if CASH_LINE_MARKER not in failure]
            row["finding_count"] = len(on_detail)
            row["cash_line_finding_count"] = len(on_cash)

        check_rows.append(row)

    return {
        "title": "Engine 5 - Validation",
        "description": "Completed pre-posting validation",
        "overall_status": _status_str(processing, default="UNKNOWN") if processing else "UNKNOWN",
        "gate_rule": "All blocking checks must pass",
        "checks": check_rows,
    }
"""Placeholder for gocoll_calculation_logic.py."""
