"""CORP GOCOLL JE - MS Agent Framework Workflow.

Wires the GOCOLL executor nodes into a WorkflowBuilder DAG that mirrors the
GOCOLL business stages while preserving the
Duke platform run-record contract and WorkflowOutput shape.

    triage -[data_complete]-> extraction -> classification -> je_assembly
           -[blocking_errors]-> halt_missing
    finalize + db_writer + efis_output + validation + reconciliation
                                                     |
                                          [FAILED] halt_computation

``HaltExecutor``, ``FinalizeExecutor``, ``OutputArtifact`` and ``WorkflowOutput``
are reused as-is.

All Azure OpenAI / vision work runs inside the ``duke-api`` container - the host
proxy (Zscaler) blocks egress to the AOAI endpoint, so a host run with no client
exercises only the text tier + fallbacks.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_framework import Executor, Workflow, WorkflowBuilder, WorkflowContext, handler

from src.source_parsers.gocoll_inputs import classify_inputs, validate_inputs
from src.workflow.executors.output_generators import (
    DBWriterExecutor,
    FinalizeExecutor,
    HaltExecutor,
)
from src.workflow.models import ComputationResult, OutputArtifact

logger = logging.getLogger(__name__)

_EXTRACTION_PROGRESS_MESSAGE = "Extracting transactions from batch PDFs..."


def _extraction_progress_interval_seconds() -> float:
    raw_interval = os.getenv("GOCOLL_EXTRACTION_PROGRESS_INTERVAL_SECONDS", "15")
    try:
        return max(1.0, float(raw_interval))
    except (TypeError, ValueError):
        return 15.0


async def _run_extraction_with_progress(
    extraction_coro: Any,
    *,
    interval_seconds: float | None = None,
) -> Any:
    """Await PDF/OCR extraction while refreshing its active progress event."""
    interval = (
        interval_seconds
        if interval_seconds is not None
        else _extraction_progress_interval_seconds()
    )

    async def _heartbeat() -> None:
        from src.logic.progress_context import emit_stage_progress

        loop = asyncio.get_running_loop()
        started = loop.time()
        while True:
            await asyncio.sleep(interval)
            try:
                elapsed_seconds = max(1, round(loop.time() - started))
                message = (
                    f"{_EXTRACTION_PROGRESS_MESSAGE} "
                    f"({elapsed_seconds}s elapsed)"
                )
                # ``detail`` is the stable option value, so EventService
                # refreshes the existing extraction option in place (timer only
                # in the label) instead of appending a new one per heartbeat.
                await emit_stage_progress(
                    "extraction",
                    "active",
                    message,
                    detail=_EXTRACTION_PROGRESS_MESSAGE,
                )
                logger.info(
                    "GOCOLL extraction heartbeat emitted: "
                    "extraction_elapsed=%ds",
                    elapsed_seconds,
                )
            except Exception:  # noqa: BLE001 - progress I/O must not abort OCR
                logger.warning(
                    "Failed to emit GOCOLL extraction progress",
                    exc_info=True,
                )

    heartbeat_task = asyncio.create_task(_heartbeat())
    try:
        return await extraction_coro
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)


# -- Triage output -----------------------------------------------------------


@dataclass
class GoCollTriageResult:
    """Output of GoCollTriageExecutor - classification + completeness."""

    batch_pdfs: list[str] = field(default_factory=list)
    go_form_paths: list[str] = field(default_factory=list)
    validation_tab: str = ""
    treasury: str = ""
    wf_report: str = ""
    unrecognised: list[str] = field(default_factory=list)
    missing_data: list[dict] = field(default_factory=list)
    has_blocking_errors: bool = False
    period_label: str = ""
    journal_date: str = ""


@dataclass
class GoCollExtractionStage:
    """Extraction-stage output plus parsed workbook control feeds."""

    triage: GoCollTriageResult
    extraction: Any
    wf_report: Any = None
    treasury_report: Any = None
    validation_master: Any = None


@dataclass
class GoCollAssemblyStage:
    """Assembly-stage output and source artifacts needed downstream."""

    triage: GoCollTriageResult
    extraction: Any
    assembly: Any
    wf_report: Any = None
    treasury_report: Any = None
    validation_master: Any = None


@dataclass
class GoCollReconciliationStage:
    """Reconciliation-stage output and source artifacts needed for validation."""

    triage: GoCollTriageResult
    extraction: Any
    assembly: Any
    reconciliation: Any = None
    wf_report: Any = None
    treasury_report: Any = None
    validation_master: Any = None


# -- Feed parsing helper -----------------------------------------------------


def _safe_parse(label: str, path: str, parser):
    """Parse a resolved feed path, logging and swallowing any failure.

    Returns the parser's result, or ``None`` when the path is empty or parsing
    raises - the pipeline then degrades to its fallback controls rather than
    aborting the run.
    """
    if not path:
        return None
    try:
        return parser(path)
    except Exception:  # noqa: BLE001 - feed parsing is best-effort
        logger.warning("GOCOLL: failed to parse %s from %s", label, path, exc_info=True)
        return None


# -- Azure OpenAI client -----------------------------------------------------


@functools.lru_cache(maxsize=1)
def _build_aoai_client():
    """Build the vision chat client (None disables the vision tier).

    Delegates to ``ms_duke_je_common.llm_provider.get_vision_chat_client`` so the
    backend is selected by ``LLM_PROVIDER`` (ACS RULE 21 / triple-agnostic) and
    the vendor SDK + managed-identity fallback live only in common ($0.5, keeping
    ``openai``/``azure`` out of agent code). Used as the fallback when no governed
    ``ctx.services.vision`` is present in the request context (e.g. unit tests or
    the legacy path); the executor normally supplies the governed client via the
    VisionService. Any failure degrades gracefully to the text tier.
    """
    from ms_duke_je_common.llm_provider import get_vision_chat_client

    return get_vision_chat_client(max_retries=1, timeout=60.0)


# -- Period auto-detection from filenames ------------------------------------
# Period precedence: caller-supplied > auto-detected from filenames.

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

_FULL_MONTH_PAT = re.compile(
    r"(?P<month>" + "|".join(_MONTH_NAMES.keys()) + r")\s+(?P<year>\d{4})", re.IGNORECASE
)
# MMYY codes like "0126" (Jan 2026) or "1225" (Dec 2025)
_MMYY_PAT = re.compile(r"(?<!\d)(0[1-9]|1[0-2])(\d{2})(?!\d)")


def _detect_period_from_filenames(filenames: list[str]) -> datetime | None:
    """Try to auto-detect the pipeline period from uploaded filenames.

    Strategy: look for "January 2026" style or "0126" MMYY codes.
    Pick the most frequently occurring period.
    """
    from collections import Counter
    periods: list[datetime] = []

    for name in filenames:
        # Try full month name first: "February 2026"
        m = _FULL_MONTH_PAT.search(name)
        if m:
            month = _MONTH_NAMES[m.group(1).lower()]
            year = int(m.group(2))
            periods.append(datetime(year, month, 1))
            continue

        # Try MMYY code: "0126" -> Jan 2026
        for mm in _MMYY_PAT.finditer(name):
            month = int(mm.group(1))
            yy = int(mm.group(2))
            year = 2000 + yy
            periods.append(datetime(year, month, 1))

    if not periods:
        return None

    # Pick the most common period; if tied, pick the latest
    counter = Counter(periods)
    most_common = counter.most_common()
    best = max(most_common, key=lambda x: (x[1], x[0]))
    return best[0]


def _classified_fm_urls(input_set: Any, fm_url_by_path: dict[str, str]) -> dict[str, str]:
    """Map triage-classified support feeds back to their canonical FilePart URLs."""
    def _url_for(path: Any) -> str:
        if not path:
            return ""
        return str(fm_url_by_path.get(str(Path(path).resolve())) or "")

    return {
        hint: url
        for hint, url in (
            ("validation", _url_for(input_set.validation_tab)),
            ("transaction", _url_for(input_set.wf_report)),
            ("treasury", _url_for(input_set.treasury)),
        )
        if url
    }


# -- Executor nodes ----------------------------------------------------------


class GoCollTriageExecutor(Executor):
    """Classify GOCOLL source files (batch PDFs vs GO forms) and check completeness."""

    def __init__(self) -> None:
        super().__init__(id="triage")

    @handler
    async def triage(
        self, message: list[str] | dict, ctx: WorkflowContext[GoCollTriageResult]
    ) -> None:
        explicit_go_forms: list[str] | None = None
        period_label = ""
        journal_date = ""

        if isinstance(message, dict):
            file_paths = list(message.get("file_paths", []))
            run_id = message.get("run_id")
            if run_id:
                ctx.set_state("run_id", run_id)
            explicit_go_forms = message.get("go_form_paths")
            period_label = message.get("period_label", "") or ""
            journal_date = message.get("journal_date", "") or message.get("period_date", "") or ""
        else:
            file_paths = list(message)

        # Period precedence: caller-supplied > auto-detected from filenames.
        # Leaves journal_date blank if neither resolves - the agent never invents a date.
        if not journal_date:
            detected = _detect_period_from_filenames([Path(fp).name for fp in file_paths])
            if detected is not None:
                journal_date = detected.strftime("%Y-%m-%d")
                logger.info("GOCOLL period auto-detected from filenames: %s", journal_date)

        # Detect the GOCOLL feeds (filename keyword + content fallback) and
        # enforce the mandatory-file / empty-file HARD STOP before any extraction
        # runs. Treasury is optional; Validation Tab, WF report and >=1 batch PDF
        # are mandatory. A combined GOCollections workbook can satisfy the
        # Validation Tab / Treasury / WF feeds at once.
        input_set = classify_inputs(file_paths)
        errors = validate_inputs(input_set)
        fm_url_by_path = message.get("fm_url_by_path", {}) if isinstance(message, dict) else {}
        fm_url_map = _classified_fm_urls(input_set, fm_url_by_path)
        ctx.set_state("gocoll_fm_url_map", fm_url_map)

        explicit = {str(Path(p)) for p in (explicit_go_forms or [])}
        batch_pdfs = [str(p) for p in input_set.batch_pdfs if str(p) not in explicit]
        go_forms = list(explicit_go_forms or [])

        missing_data = [{"item": "input", "reason": e} for e in errors]

        result = GoCollTriageResult(
            batch_pdfs=batch_pdfs,
            go_form_paths=go_forms,
            validation_tab=str(input_set.validation_tab) if input_set.validation_tab else "",
            treasury=str(input_set.treasury) if input_set.treasury else "",
            wf_report=str(input_set.wf_report) if input_set.wf_report else "",
            unrecognised=[str(p) for p in input_set.unknown],
            missing_data=missing_data,
            has_blocking_errors=bool(missing_data),
            period_label=period_label,
            journal_date=journal_date,
        )
        ctx.set_state("gocoll_triage", result)
        logger.info(
            "GOCOLL triage: %d batch PDF(s), %d GO form(s), validation_tab=%s "
            "treasury=%s wf_report=%s, %d unrecognised, blocking=%s",
            len(batch_pdfs), len(go_forms), bool(result.validation_tab),
            bool(result.treasury), bool(result.wf_report),
            len(result.unrecognised), result.has_blocking_errors,
        )
        await ctx.send_message(result)


class GoCollExtractionExecutor(Executor):
    """Extract GOCOLL lockbox/GO-form data and parse source workbook controls."""

    def __init__(self) -> None:
        super().__init__(id="extraction")

    @handler
    async def extract(
        self, triage: GoCollTriageResult, ctx: WorkflowContext[GoCollExtractionStage]
    ) -> None:
        from src.logic.vision_context import get_current_vision_service
        from src.source_parsers.gocoll_coordinator import extract_period

        # Prefer the governed vision service (budget enforcement + usage
        # accumulation, §8.5/§6). Fall back to the local client builder when no
        # service is bound to the request context (unit tests / legacy path).
        vision = get_current_vision_service()
        aoai = vision.get_client() if vision is not None else _build_aoai_client()
        model = os.environ.get("GOCOLL_VISION_MODEL", "gpt-5.6-sol")
        fallback = os.environ.get("GOCOLL_VISION_FALLBACK_MODEL", "gpt-5.2")

        from src.source_parsers.treasury_parser import parse_treasury_report
        from src.source_parsers.wf_report_parser import parse_wf_report
        from src.validation.validation_tab_master import (
            load_validation_tab_from_workbook,
        )

        # These workbook reads are independent and read-only. Parse them in
        # parallel so a combined source workbook does not make the WF,
        # Treasury, and Validation startup cost additive. Each parser owns its
        # own openpyxl workbook instance; no workbook mutation is concurrent.
        wf_report, treasury_report, validation_master = await asyncio.gather(
            asyncio.to_thread(
                _safe_parse, "WF report", triage.wf_report, parse_wf_report
            ),
            asyncio.to_thread(
                _safe_parse, "Treasury report", triage.treasury, parse_treasury_report
            ),
            asyncio.to_thread(
                _safe_parse,
                "Validation Tab",
                triage.validation_tab,
                load_validation_tab_from_workbook,
            ),
        )

        try:
            extraction = await _run_extraction_with_progress(
                extract_period(
                    triage.batch_pdfs,
                    period_label=triage.period_label,
                    journal_date=triage.journal_date,
                    aoai_client=aoai,
                    model=model,
                    fallback_model=fallback,
                    go_form_paths=triage.go_form_paths or None,
                )
            )
        except Exception as exc:  # noqa: BLE001 - surface as FAILED, do not crash workflow
            logger.exception("GOCOLL extraction raised")
            await ctx.send_message(
                ComputationResult(status="FAILED", errors=[str(exc)])
            )
            return

        # Fold the aggregated vision usage into the governed service so the
        # framework accumulates ``ctx.response.llm_usage`` and enforces the run
        # budget ($8.5/$8.6). Soft mode (no limiter) never raises; hard mode
        # surfaces BudgetExceededError once the page fan-out has completed.
        if vision is not None:
            vision.record_usage(
                getattr(extraction, "prompt_tokens", 0) or 0,
                getattr(extraction, "completion_tokens", 0) or 0,
                getattr(extraction, "llm_calls", 0) or 0,
            )

        # Check-level reconciliation + extracted-data correction, before assembly
        # so digit-repair corrections flow into the JE and Extracted Data.
        try:
            from src.validation.gocoll_check_reconciliation import reconcile_and_correct
            reconcile_and_correct(extraction, wf_report)
        except Exception:  # noqa: BLE001 - correction is best-effort, never fatal
            logger.warning("GOCOLL check-level reconciliation failed", exc_info=True)

        await ctx.send_message(
            GoCollExtractionStage(
                triage=triage,
                extraction=extraction,
                wf_report=wf_report,
                treasury_report=treasury_report,
                validation_master=validation_master,
            )
        )


class GoCollClassificationExecutor(Executor):
    """Explicit workflow node for classification readiness.

    Classification is performed by the assembler's codeblock classifier, so this
    node keeps the workflow graph explicit without changing business behavior.
    """

    def __init__(self) -> None:
        super().__init__(id="classification")

    @handler
    async def classify(
        self,
        stage: GoCollExtractionStage,
        ctx: WorkflowContext[GoCollExtractionStage],
    ) -> None:
        from src.logic.progress_context import emit_stage_progress
        await emit_stage_progress("classification", "active", "Classifying GO Collection codes...")
        await ctx.send_message(stage)


class GoCollJEAssemblyExecutor(Executor):
    """Assemble the GOCOLL journal entry from extracted transactions."""

    def __init__(self) -> None:
        super().__init__(id="je_assembly")

    @handler
    async def assemble(
        self,
        stage: GoCollExtractionStage,
        ctx: WorkflowContext[GoCollAssemblyStage],
    ) -> None:
        from src.assembly.gocoll_assembler import assemble_journal_entry
        from src.logic.progress_context import emit_stage_progress
        await emit_stage_progress("je_assembly", "active", "Assembling journal entries...")

        assembly = await asyncio.to_thread(
            assemble_journal_entry,
            stage.extraction,
            otc_amount=(
                stage.treasury_report.total_over_the_counter
                if stage.treasury_report is not None
                else None
            ),
        )

        await ctx.send_message(
            GoCollAssemblyStage(
                triage=stage.triage,
                extraction=stage.extraction,
                assembly=assembly,
                wf_report=stage.wf_report,
                treasury_report=stage.treasury_report,
                validation_master=stage.validation_master,
            )
        )


class GoCollReconciliationExecutor(Executor):
    """Build A/B/C reconciliation details for output tabs and validation context."""

    def __init__(self) -> None:
        super().__init__(id="reconciliation")

    @handler
    async def reconcile(
        self,
        stage: GoCollAssemblyStage,
        ctx: WorkflowContext[GoCollReconciliationStage],
    ) -> None:
        from src.validation.gocoll_reconciliation_check import reconcile_batches
        from src.logic.progress_context import emit_stage_progress
        await emit_stage_progress("reconciliation", "active", "Reconciling to Treasury control total...")

        reconciliation = await asyncio.to_thread(
            reconcile_batches,
            stage.assembly,
            wf_report=stage.wf_report,
            treasury_report=stage.treasury_report,
            extraction=stage.extraction,
        )
        await ctx.send_message(
            GoCollReconciliationStage(
                triage=stage.triage,
                extraction=stage.extraction,
                assembly=stage.assembly,
                reconciliation=reconciliation,
                wf_report=stage.wf_report,
                treasury_report=stage.treasury_report,
                validation_master=stage.validation_master,
            )
        )


class GoCollValidationExecutor(Executor):
    """Run the rule-pack validation gate and emit a ComputationResult."""

    def __init__(self) -> None:
        super().__init__(id="validation")

    @handler
    async def validate(
        self,
        stage: GoCollReconciliationStage,
        ctx: WorkflowContext[ComputationResult],
    ) -> None:
        from src.gocoll_pipeline import GoCollPipelineResult
        from src.logic.progress_context import emit_stage_progress
        from src.models.domain import CheckResult
        from src.models.enums import ValidationStatus
        from src.validation.gocoll_validation_engine import validate_assembly
        await emit_stage_progress("validation", "active", "Validating journal entries...")

        extraction = stage.extraction
        assembly = stage.assembly
        processing_result = await asyncio.to_thread(
            validate_assembly,
            assembly,
            extraction,
            wf_report=stage.wf_report,
            treasury_report=stage.treasury_report,
            validation_master=stage.validation_master,
        )

        if stage.triage.batch_pdfs and extraction.transaction_count == 0:
            detail = (
                "No transactions were extracted from the supplied batch PDF(s); "
                "this usually means the vision tier failed or returned no usable rows."
            )
            processing_result.status = ValidationStatus.FAILED
            processing_result.checks.append(
                CheckResult(
                    check_name="extraction_nonempty",
                    status=ValidationStatus.FAIL,
                    details=detail,
                    failures=list(extraction.errors) or [detail],
                )
            )
            processing_result.failure_reasons.append(detail)

        if extraction.errors:
            detail = (
                f"Extraction completed with {len(extraction.errors)} page/batch error(s); "
                "the JE may be partial and must not be marked verified."
            )
            processing_result.status = ValidationStatus.FAILED
            processing_result.checks.append(
                CheckResult(
                    check_name="extraction_errors",
                    status=ValidationStatus.FAIL,
                    details=detail,
                    failures=list(extraction.errors),
                )
            )
            processing_result.failure_reasons.append(detail)

        review_items = extraction.review_items
        if review_items:
            failures = [
                f"Batch {t.batch_number} seq {t.sequence} check {t.check_amount}: "
                f"{t.review_reason}"
                for t in review_items
            ]
            processing_result.checks.append(
                CheckResult(
                    check_name="reconciliation_review",
                    status=ValidationStatus.REVIEW,
                    details=(
                        f"{len(review_items)} transaction(s) could not be reconciled "
                        f"to their check total and need analyst review before posting"
                    ),
                    failures=failures,
                )
            )
            logger.warning(
                "GOCOLL reconciliation: %d transaction(s) flagged for human review",
                len(review_items),
            )

        # Reformatting is recorded (not gated): the posted value is already the
        # eFIS form, and the analyst needs the audit trail of what was rewritten.
        reformatted = [w for w in assembly.warnings if "reformatted to" in w]
        if reformatted:
            processing_result.checks.append(
                CheckResult(
                    check_name="codeblock_format",
                    status=ValidationStatus.PASS,
                    details=(
                        f"{len(reformatted)} code block value(s) rewritten to the "
                        "eFIS form before posting"
                    ),
                    failures=reformatted,
                )
            )
            logger.info(
                "GOCOLL codeblock format: %d value(s) normalized for eFIS",
                len(reformatted),
            )

        result = GoCollPipelineResult(
            extraction=extraction,
            assembly=assembly,
            processing_result=processing_result,
            warnings=list(extraction.warnings) + list(assembly.warnings),
            reconciliation=stage.reconciliation,
            wf_report=stage.wf_report,
            treasury_report=stage.treasury_report,
            validation_master=stage.validation_master,
        )

        je = result.assembly.journal_entry
        _ex = result.extraction
        summary = {
            "period_label": stage.triage.period_label,
            "journal_date": stage.triage.journal_date,
            "batch_count": len(result.extraction.batches),
            "transaction_count": result.extraction.transaction_count,
            "line_count": je.line_count if je else 0,
            "monetary_total": str(je.monetary_total) if je else None,
            "balanced": result.assembly.is_balanced,
            "status": result.status,
            "checks": [
                {"name": c.check_name, "passed": c.passed, "detail": c.details}
                for c in result.processing_result.checks
            ],
            # Vision LLM usage - consumed by the executor's governance cost
            # tracker/rate limiter to emit cost_summary + llm_budget.
            "llm_usage": {
                "prompt_tokens": int(getattr(_ex, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(_ex, "completion_tokens", 0) or 0),
                "total_tokens": int(
                    (getattr(_ex, "prompt_tokens", 0) or 0)
                    + (getattr(_ex, "completion_tokens", 0) or 0)
                ),
                "llm_calls": int(getattr(_ex, "llm_calls", 0) or 0),
                "model": str(getattr(_ex, "vision_model", "") or ""),
            }
        }
        ctx.set_state("pipeline_summary", summary)

        await ctx.send_message(
            ComputationResult(
                status=result.status,
                pipeline_result=result,
                errors=list(result.extraction.errors),
            )
        )


class GoCollEfisOutputExecutor(Executor):
    """Build the GOCOLL eFIS upload rows artifact from a VERIFIED pipeline."""

    def __init__(self) -> None:
        super().__init__(id="gocoll_efis_output")

    @handler
    async def generate(
        self, result: ComputationResult, ctx: WorkflowContext[OutputArtifact]
    ) -> None:
        from src.logic.progress_context import emit_stage_progress
        # Hand off to the shared ERP agent. Activating "done" here would make
        # the monotonic stepper mark ERP complete before ERP has even started.
        await emit_stage_progress("erp", "active", "Preparing ERP output...")
        run_id = ctx.get_state("run_id") or "unknown"

        if result.status != "VERIFIED" or result.pipeline_result is None:
            await ctx.send_message(
                OutputArtifact(artifact_type="working_file", error="Pipeline not VERIFIED")
            )
            return

        pipeline = result.pipeline_result
        je = pipeline.assembly.journal_entry
        efis_rows = pipeline.efis_rows

        # Per-row metadata (index-aligned with efis_rows) + per-check printed
        # totals so the eFIS renderer can flag/highlight reconciliation
        # exceptions per CHECK rather than per batch. ``je.all_lines`` is built
        # in the same order as ``efis_rows`` (cash line then detail/plug lines,
        # per batch), so the two lists align position-for-position.
        efis_row_meta: list[dict] = []
        if je is not None:
            for line in je.all_lines:
                efis_row_meta.append(
                    {
                        "line_kind": getattr(line, "line_kind", "") or "",
                        "batch_number": getattr(line, "batch_number", "") or "",
                        "check_number": getattr(line, "check_number", "") or "",
                        "classification": getattr(line, "classification", "") or "",
                        "line_seq": getattr(line, "line_seq", None),
                    }
                )

        # Printed check totals keyed by ``{batch}{check_number}``. The parent
        # transaction carries the full deposited check amount; its split sub-lines
        # carry 0, so the per-check total is the max over that check's rows.
        check_totals: dict[str, float] = {}
        for b in pipeline.extraction.batches:
            for t in b.transactions:
                cn = getattr(t, "check_number", "") or ""
                if not cn:
                    continue
                try:
                    amt = abs(float(t.check_amount or 0))
                except (TypeError, ValueError):
                    amt = 0.0
                if amt:
                    key = f"{b.batch_number}|{cn}"
                    check_totals[key] = max(check_totals.get(key, 0.0), amt)

        build_output = {
            "erp_type": "gocoll",
            "run_id": run_id,
            "period_label": pipeline.extraction.period_label,
            "journal_date": pipeline.extraction.journal_date,
            "efis_rows": efis_rows,
            "efis_row_meta": efis_row_meta,
            "check_totals": check_totals,
            "line_count": je.line_count if je else 0,
            "monetary_total": str(je.monetary_total) if je else None,
            # $|monetary| across all lines - the Sheet1 "Control Amount" figure.
            "control_amount": (
                str(sum(abs(line.monetary_amount) for line in je.all_lines), Decimal("0"))
                if je else None
            ),
            "balanced": pipeline.assembly.is_balanced,
            "batches": [
                {
                    "batch_number": b.batch_number,
                    "transaction_count": len(b.transactions),
                    "check_total": str(b.check_total),
                }
                for b in pipeline.extraction.batches
            ],
        }

        # Extracted Data payload -> rendered as a review tab by the shared renderer.
        try:
            from src.output.gocoll_output_builders import (
                RECON_FLAG_TYPES,
                build_ctrl_vs_wf_check_rows,
                build_ctrl_vs_wf_rows,
                build_extracted_data_rows,
                build_flags_rows,
                build_support_sources,
            )
            (
                build_output["extracted_data"],
                build_output["ctrl_vs_wf"],
                build_output["ctrl_vs_wf_checks"],
                all_flags,
            ) = await asyncio.gather(
                asyncio.to_thread(build_extracted_data_rows, pipeline),
                asyncio.to_thread(build_ctrl_vs_wf_rows, pipeline),
                asyncio.to_thread(build_ctrl_vs_wf_check_rows, pipeline),
                asyncio.to_thread(build_flags_rows, pipeline),
            )
            triage = ctx.get_state("gocoll_triage")
            validation_source = getattr(triage, "validation_tab", "") if triage else ""
            fm_url_map: dict[str, str] = ctx.get_state("gocoll_fm_url_map") or {}
            build_output["support_sources"] = await asyncio.to_thread(
                build_support_sources,
                validation_source,
                getattr(pipeline.wf_report, "source_file", "")
                if pipeline.wf_report else "",
                getattr(pipeline.treasury_report, "source_file", "")
                if pipeline.treasury_report else "",
                fm_url_map=fm_url_map,
            )
            # Exclude reconciliation flags; GO form + validation flags remain.
            build_output["flags"] = [f for f in all_flags if f.get("type") not in RECON_FLAG_TYPES]
        except Exception:
            logger.warning("Failed to build GOCOLL extra output tabs", exc_info=True)
            build_output["extracted_data"] = []
            build_output["ctrl_vs_wf"] = []
            build_output["ctrl_vs_wf_checks"] = []
            build_output["flags"] = []
            build_output["support_sources"] = []

        # Attach Calculation Logic + Explainability payloads for the UI tabs.
        # Built deterministically from the pipeline result; failures are
        # non-fatal so the eFIS output still ships.
        try:
            from src.gocoll_calculation_logic import build_gocoll_calculation_logic
            build_output["calculation_logic"] = await asyncio.to_thread(
                build_gocoll_calculation_logic,
                pipeline,
                run_id=run_id,
                period_label=pipeline.extraction.period_label,
                journal_date=pipeline.extraction.journal_date,
            )
        except Exception:
            logger.warning("Failed to build GOCOLL calculation logic", exc_info=True)
            build_output["calculation_logic"] = None

        try:
            from src.gocoll_explainability import build_gocoll_explainability
            build_output["explainability_report"] = await asyncio.to_thread(
                build_gocoll_explainability,
                pipeline,
                run_id=run_id,
                period_label=pipeline.extraction.period_label,
                journal_date=pipeline.extraction.journal_date,
            )
        except Exception:
            logger.warning("Failed to build GOCOLL explainability report", exc_info=True)
            build_output["explainability_report"] = None

        ctx.set_state("build_output_data", build_output)

        logger.info(
            "GOCOLL eFIS output: run=%s, %d row(s), monetary_total=%s",
            run_id, len(efis_rows), build_output["monetary_total"],
        )
        await ctx.send_message(
            OutputArtifact(artifact_type="working_file", file_path=None)
        )


# -- Edge condition predicates -----------------------------------------------


def _triage_has_blocking_errors(msg: Any) -> bool:
    if isinstance(msg, GoCollTriageResult):
        return msg.has_blocking_errors
    return False


def _triage_data_complete(msg: Any) -> bool:
    if isinstance(msg, GoCollTriageResult):
        return not msg.has_blocking_errors
    return True


def _computation_verified(msg: Any) -> bool:
    if isinstance(msg, ComputationResult):
        return msg.status == "VERIFIED"
    return False


def _computation_failed(msg: Any) -> bool:
    if isinstance(msg, ComputationResult):
        return msg.status != "VERIFIED"
    return False


def _extraction_succeeded(msg: Any) -> bool:
    return isinstance(msg, GoCollExtractionStage)


# -- Workflow factory --------------------------------------------------------


def build_gocoll_workflow() -> Workflow:
    """Build the full GOCOLL JE workflow DAG.

    Returns a fresh Workflow instance with independent executor state.
    Call ``workflow.run({...})`` to execute.
    """
    triage = GoCollTriageExecutor()
    halt_missing = HaltExecutor("missing_data")
    extraction = GoCollExtractionExecutor()
    classification = GoCollClassificationExecutor()
    je_assembly = GoCollJEAssemblyExecutor()
    reconciliation = GoCollReconciliationExecutor()
    validation = GoCollValidationExecutor()
    halt_computation = HaltExecutor("computation")
    efis_output = GoCollEfisOutputExecutor()
    db_writer = DBWriterExecutor()
    finalize = FinalizeExecutor()

    workflow = (
        WorkflowBuilder(
            start_executor=triage,
            output_from=[finalize, halt_missing, halt_computation],
        )
        # Triage -> halt or pipeline
        .add_edge(triage, halt_missing, condition=_triage_has_blocking_errors)
        .add_edge(triage, extraction, condition=_triage_data_complete)
        # Business stages
        .add_edge(extraction, classification, condition=_extraction_succeeded)
        .add_edge(extraction, halt_computation, condition=_computation_failed)
        .add_edge(classification, je_assembly)
        .add_edge(je_assembly, reconciliation)
        .add_edge(reconciliation, validation)
        # Validation -> output fan-out or halt
        .add_edge(validation, efis_output, condition=_computation_verified)
        .add_edge(validation, db_writer, condition=_computation_verified)
        .add_edge(validation, halt_computation, condition=_computation_failed)
        # Output generators + finalize (fan-in delivers list[OutputArtifact])
        .add_fan_in_edges([efis_output, db_writer], finalize)
        .build()
    )
    return workflow


# -- Convenience runner ------------------------------------------------------


async def run_gocoll_workflow(
    file_paths: list[str],
    *,
    user_id: str | None = None,
    period_date: str | None = None,
    period_label: str | None = None,
    run_id: str | None = None,
    go_form_paths: list[str] | None = None,
    fm_url_by_path: dict[str, str] | None = None,
) -> dict:
    """High-level entry point for the GOCOLL service, conforming to the Duke
    platform run-record contract.

    Args:
        file_paths: Source file paths (lockbox batch PDFs and optional GO forms).
        user_id: Optional user identifier for audit trail.
        period_date: eFIS journal date (``YYYY-MM-DD``).
        period_label: eFIS header label (e.g. ``"JAN2026_BATCH:632-633"``).
        run_id: Reuse an existing scheduler/API run_id when supplied.
        go_form_paths: Optional separately-supplied GO Collection Form files
            carrying the coded GL distribution split.

    Returns:
        Dict with run_id, status, artifacts, errors, summary, and _build_output.
    """
    resolved_run_id = run_id or uuid4().hex

    workflow = build_gocoll_workflow()

    events = await workflow.run({
        "file_paths": file_paths,
        "run_id": resolved_run_id,
        "period_date": period_date,
        "period_label": period_label,
        "go_form_paths": go_form_paths,
        "fm_url_by_path": fm_url_by_path or {},
    })

    outputs = events.get_outputs()
    if outputs:
        result = outputs[0]
        build_output_data = getattr(result, "build_output_data", None)
        rv: dict[str, Any] = {
            "run_id": getattr(result, "run_id", resolved_run_id),
            "status": getattr(result, "status", "UNKNOWN"),
            "artifacts": [
                {"type": a.artifact_type, "path": a.file_path, "error": a.error}
                for a in getattr(result, "artifacts", [])
            ],
            "errors": getattr(result, "errors", []),
            "summary": getattr(result, "summary", {}),
        }
        if build_output_data:
            rv["_build_output"] = build_output_data
        return rv

    return {
        "run_id": resolved_run_id,
        "status": "UNKNOWN",
        "artifacts": [],
        "errors": ["No workflow output produced"],
    }