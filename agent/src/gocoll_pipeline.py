"""CORP GOCOLL JE Pipeline - 4-Engine Business Logic.

Wires the GOCOLL engines in sequence:

    1. Extraction   - gocoll_coordinator.extract_period (text + vision tiers)
    2. Classification - codeblock_classifier (run inside Engine 3)
    3. JE Assembly  - gocoll_assembler.assemble_journal_entry (cash + details)
    4. Validation   - gocoll_validation_engine.validate_assembly (rule-pack gate)

This is the canonical entry point for the GOCOLL service.

All Azure OpenAI / vision work runs inside the ``duke-api`` container -
the host proxy (Zscaler) blocks egress to the AOAI endpoint, so a host
run with ``aoai_client=None`` exercises only the text tier + fallbacks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.assembly.gocoll_assembler import assemble_journal_entry
from src.models.domain import CheckResult, ProcessingResult
from src.models.enums import ValidationStatus
from src.models.gocoll_assembly_models import GoCollAssemblyResult
from src.models.gocoll_models import GoCollExtraction
from src.models.gocoll_reconciliation_models import ReconciliationResult
from src.models.gocoll_source_models import TreasuryReport, WFReport
from src.source_parsers.gocoll_coordinator import extract_period
from src.validation.gocoll_check_reconciliation import reconcile_and_correct
from src.validation.gocoll_reconciliation_check import reconcile_batches
from src.validation.gocoll_validation_engine import validate_assembly

logger = logging.getLogger(__name__)


@dataclass
class GoCollPipelineResult:
    """Complete GOCOLL pipeline output with intermediate artifacts."""

    extraction: GoCollExtraction
    assembly: GoCollAssemblyResult
    processing_result: ProcessingResult
    warnings: list[str] = field(default_factory=list)
    # A/B/C batch reconciliation (populated when WF/Treasury feeds are supplied).
    reconciliation: ReconciliationResult | None = None
    wf_report: WFReport | None = None
    treasury_report: TreasuryReport | None = None
    # Validation Tab master (drives Sheet1 dimension dropdowns in the renderer).
    validation_master: Any = None

    @property
    def status(self) -> str:
        return self.processing_result.status

    @property
    def efis_rows(self) -> list[dict]:
        return self.assembly.efis_rows


async def run_pipeline(
    pdf_paths: list[str | Path],
    *,
    period_label: str = "",
    journal_date: str = "",
    aoai_client=None,
    model: str = "gpt-5.2",
    fallback_model: str | None = "gpt-5.2",
    image_dpi: int = 300,
    go_form_paths: list[str | Path] | None = None,
    wf_report: WFReport | None = None,
    treasury_report: TreasuryReport | None = None,
    validation_master=None,
) -> GoCollPipelineResult:
    """Execute the full GOCOLL JE pipeline.

    Args:
        pdf_paths: The lockbox batch PDFs for the period.
        period_label: eFIS header label, e.g. "JAN2025_BATCH-632-633".
        journal_date: eFIS journal date, e.g. "2026-01-31".
        aoai_client: Azure OpenAI async client (None disables the vision tier).
        model / fallback_model: Vision deployments.
        image_dpi: Render DPI for page images.
        go_form_paths: Optional separately-supplied GO Collection Form files
            (PDF or image) carrying the coded GL distribution split. When
            present they override the matching batch's text-tier coding.

    Returns:
        A :class:`GoCollPipelineResult` with status VERIFIED or FAILED.
    """
    logger.info(
        "GOCOLL pipeline start - period=%s, journal_date=%s, %d PDF(s)",
        period_label or "(unset)", journal_date or "(unset)", len(pdf_paths),
    )

    # -- Engine 1: Extraction ------------------------------------------------
    logger.info("Engine 1: Extraction")
    extraction = await extract_period(
        pdf_paths,
        period_label=period_label,
        journal_date=journal_date,
        aoai_client=aoai_client,
        model=model,
        fallback_model=fallback_model,
        image_dpi=image_dpi,
        go_form_paths=go_form_paths,
    )
    logger.info(
        "Engine 1 done: %d batch(es), %d transaction(s), %d error(s)",
        len(extraction.batches), extraction.transaction_count, len(extraction.errors),
    )

    # Check-level reconciliation + extracted-data correction (before assembly so
    # digit-repair corrections flow into the JE and Extracted Data).
    from src.validation.gocoll_check_reconciliation import reconcile_and_correct
    reconcile_and_correct(extraction, wf_report)

    # -- Engines 2-3: Classification + JE Assembly ---------------------------
    logger.info("Engines 2-3: Classification + JE Assembly")
    assembly = assemble_journal_entry(
        extraction,
        otc_amount=(
            treasury_report.total_over_the_counter
            if treasury_report is not None
            else None
        ),
    )
    je = assembly.journal_entry
    logger.info(
        "Engine 3 done: %d line(s), monetary_total=%s, balanced=%s",
        je.line_count if je else 0,
        je.monetary_total if je else "n/a",
        assembly.is_balanced,
    )

    # -- Engine 5: Validation ------------------------------------------------
    # (Engine 4 = Reconciliation runs as a separate workflow node before this.)
    logger.info("Engine 5: Validation")
    processing_result = validate_assembly(
        assembly,
        extraction,
        wf_report=wf_report,
        treasury_report=treasury_report,
        validation_master=validation_master,
    )
    logger.info(
        "Engine 5 done: status=%s, %d check(s)",
        processing_result.status, len(processing_result.checks),
    )

    if pdf_paths and extraction.transaction_count == 0:
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

    # A/B/C reconciliation detail (for the Ctrl-Vs-WF + Flags outputs).
    reconciliation = reconcile_batches(
        assembly,
        wf_report=wf_report,
        treasury_report=treasury_report,
        extraction=extraction,
    )

    # Surface G0-form reconciliation exceptions as a tracked gate check so a
    # low-scan-quality misread is routed to human review rather than posting
    # silently behind a balancing plug. The JE stays balanced/postable; the
    # REVIEW check must be cleared by an analyst before posting (HOTL).
    review_items = extraction.review_items
    if review_items:
        failures = [
            f"batch {t.batch_number} seq {t.sequence} check {t.check_amount}: "
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

    warnings = list(extraction.warnings) + list(assembly.warnings)
    return GoCollPipelineResult(
        extraction=extraction,
        assembly=assembly,
        processing_result=processing_result,
        warnings=warnings,
        reconciliation=reconciliation,
        wf_report=wf_report,
        treasury_report=treasury_report,
        validation_master=validation_master,
    )