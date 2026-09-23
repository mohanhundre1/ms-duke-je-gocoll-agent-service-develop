"""Output/terminal executors used by the GOCOLL workflow.

``DBWriterExecutor`` persists run status + validation checks, ``HaltExecutor``
terminates on blocking errors, and ``FinalizeExecutor`` collects artifacts and
yields the ``WorkflowOutput``.
"""

from __future__ import annotations

import logging
from typing import Any, Never

from agent_framework import Executor, WorkflowContext, handler
from src.workflow.models import ComputationResult, OutputArtifact, WorkflowOutput

logger = logging.getLogger(__name__)


class DBWriterExecutor(Executor):
    """No-op writer - run persistence removed."""

    def __init__(self) -> None:
        super().__init__(id="db_writer")

    @handler
    async def write(
        self, result: ComputationResult, ctx: WorkflowContext[OutputArtifact]
    ) -> None:
        run_id = ctx.get_state("run_id")
        await ctx.send_message(OutputArtifact("database", file_path=f"run:{run_id}" if run_id else None))


class HaltExecutor(Executor):
    """Terminal node for halting on blocking errors."""

    def __init__(self, halt_reason: str = "blocking_error") -> None:
        super().__init__(id=f"halt:{halt_reason}")
        self._reason = halt_reason

    @handler
    async def halt(
        self, data: Any, ctx: WorkflowContext[Never, WorkflowOutput]
    ) -> None:
        run_id = ctx.get_state("run_id") or "unknown"
        errors: list[str] = []

        if hasattr(data, "missing_data"):
            errors = [m.get("reason", str(m)) for m in data.missing_data]
        elif hasattr(data, "blocking_errors"):
            errors = data.blocking_errors
        elif hasattr(data, "errors"):
            errors = data.errors
        else:
            errors = [f"Halted: {self._reason}"]

        logger.warning("Workflow HALTED (run=%s): %s", run_id, errors)

        await ctx.yield_output(
            WorkflowOutput(
                run_id=run_id,
                status="HALTED",
                errors=errors,
            )
        )


class FinalizeExecutor(Executor):
    """Terminal node - collect output artifacts and yield WorkflowOutput."""

    def __init__(self) -> None:
        super().__init__(id="finalize")

    @handler
    async def finalize(
        self, artifacts: list[OutputArtifact], ctx: WorkflowContext[Never, WorkflowOutput]
    ) -> None:
        from src.logic.progress_context import emit_stage_progress
        await emit_stage_progress("done", "active", "Finalising artifacts...")
        run_id = ctx.get_state("run_id") or "unknown"
        errors = [a.error for a in artifacts if a.error]

        # Determine status: VERIFIED if primary outputs (working_file or peoplesoft)
        # succeeded, even if secondary outputs (database) had errors
        primary_types = ("working_file", "peoplesoft")
        primary_errors = [
            a.error for a in artifacts
            if a.error and a.artifact_type in primary_types
        ]
        status = "VERIFIED" if not primary_errors else "FAILED"

        summary = ctx.get_state("pipeline_summary") or {}
        aggregated_data = ctx.get_state("aggregated_data")
        build_output_data = ctx.get_state("build_output_data")

        output = WorkflowOutput(
            run_id=run_id,
            status=status,
            artifacts=artifacts,
            errors=errors,
            summary=summary,
            aggregated_data=aggregated_data,
            build_output_data=build_output_data,
        )

        logger.info(
            "Finalize: run=%s, status=%s, %d artifacts, %d errors",
            run_id, status, len(artifacts), len(errors)
        )

        await ctx.yield_output(output)