"""Data types flowing between workflow executor nodes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models.enums import PipelineStatus

# -- Computation output ------------------------------------------------

@dataclass
class ComputationResult:
    """Wraps the pipeline result for downstream nodes."""
    status: PipelineStatus | str  # Prefer PipelineStatus enum
    pipeline_result: Any = None
    errors: list[str] = field(default_factory=list)


# -- Output generation -------------------------------------------------

@dataclass
class OutputArtifact:
    """One generated output file."""
    artifact_type: str          # "working_file" | "peoplesoft" | "audit_package"
    file_path: str | None = None
    error: str | None = None


@dataclass
class WorkflowOutput:
    """Final workflow result yielded to the caller."""
    run_id: str
    status: PipelineStatus | str  # Prefer PipelineStatus enum; "HALTED" uses string fallback
    artifacts: list[OutputArtifact] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    aggregated_data: Any = None    # raw parsed Excel/PDF for cross-check
    build_output_data: dict | None = None  # Full build_output() dict for ERP renderer