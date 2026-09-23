"""GoCollAgentLogic - the transport-neutral GOCOLL business logic.

This is the agent's terminal `AgentLogic`: the framework `PipelineExecutor`
(wired by :func:`ms_duke_je_common.entrypoints.pipeline_server.run_pipeline_agent`)
owns the protocol seam - auth, tenant/correlation ingress, inbound prompt
governance, the outbound content-safety guard, the A2A envelope (the
`uiKind=result` DataPart + terminal `aiMetrics`/`errorCode`), and the
413/content-length limit. Everything from input materialization through document
trust, the GOCOLL workflow, and result enrichment lives here, behind the
framework :class:`ExecutionContext`.

This module drives two framework seams itself:

* the §8.2 **progress channel** (`ctx.services.events`) for the pipeline
  stepper - the executor binds it to A2A `working`/`stepper` events; and
* the response hints on `ctx.response` (`ui_kind`, `success`,
  `ai_metrics`, `llm_usage`) that the executor turns into the terminal
  envelope.

Collaborators that only ship in the pinned `ms-duke-je-common` release
(`validate_documents`, `coerce_period_date`, the common LLM-token SLI) and
the workflow itself are reached through thin module-level wrappers, so this
module stays importable - and unit-testable - against older common builds and
without the workflow's heavy vision/PDF dependencies loaded at import time.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from ms_duke_je_common.context.execution import ExecutionContext
from ms_duke_je_common.executor.agent_logic import BaseAgentLogic
from ms_duke_je_common.governance.cost_tracker import CostTracker
from ms_duke_je_common.governance.rate_limiter import (
    RateLimitExceededError,
    create_rate_limiter_for_agent,
    get_agent_llm_budget_summary,
)
from ms_duke_je_common.observability import record_pipeline_duration
from ms_duke_je_common.pipeline_envelope import (
    PipelineResultEnvelope,
    PipelineValidation,
    ValidationCheck,
)
from ms_duke_je_common.reasoning import (
    FlowStatus,
    ReasoningEvent,
    ReasoningPhase,
    StageDefinition,
    StepperState,
)

# --- MISSING LINES 50-93 (Not visible in provided images) ---

from src import observability as obs
from src.logic.inputs import (
    GoCollInputs,
    materialize_base64,
    parse_inputs,
    resolve_input_file_parts,
    resolve_source_paths,
)
from src.logic.progress_context import use_progress_emitter
from src.logic.vision_context import use_vision_service
from src.models.enums import A2AErrorCode, PipelineStatus

logger = logging.getLogger(__name__)
AGENT_NAME = "gocoll-je-agent"

#: Pipeline stepper stages - must match registry flowNodes exactly.
_STAGE_DEFS: tuple[StageDefinition, ...] = (
    StageDefinition("upload", "Upload"),
    StageDefinition("extraction", "Data Extraction"),
    StageDefinition("classification", "Classification"),
    StageDefinition("je_assembly", "JE Assembly"),
    StageDefinition("reconciliation", "Reconciliation"),
    StageDefinition("validation", "Validation"),
    StageDefinition("erp", "ERP Template"),
    StageDefinition("done", "Complete"),
)


class GoCollInputError(Exception):
    """No usable GOCOLL input was supplied.

    Mapped to `INVALID_INPUT` by :func:`gocoll_error_code`; no run is recorded
    (parity with the legacy early-exit, which failed before any `record_run`).
    """


class GoCollTrustError(Exception):
    """A resolved source document failed trust validation.

    Mapped to `INVALID_INPUT` and records a `HALTED` run. The rejection SLI
    (`record_document_rejected`) is emitted here, at the trust gate, before the
    error is raised.
    """


def gocoll_error_code(exc: BaseException) -> int:
    """Map an exception to its A2A JSON-RPC error code (error taxonomy).

    Passed to `run_pipeline_agent(error_code_mapper=...)` so the framework
    stamps consistent codes onto FAILED terminal statuses (§8.8).
    """
    from ms_duke_je_common.errors import (
        AuthenticationError,
        AuthorizationError,
        BudgetExceededError,
        InputValidationError,
    )
    from ms_duke_je_common.governance import (
        ContentSafetyError,
        PathEscapeError,
        UriValidationError,
    )

    if isinstance(exc, ContentSafetyError):
        return int(A2AErrorCode.CONTENT_SAFETY)
    if isinstance(exc, (RateLimitExceededError, BudgetExceededError)):
        return int(A2AErrorCode.POLICY_DENIED)
    if isinstance(
        exc,
        (
            PathEscapeError,
            UriValidationError,
            InputValidationError,
            GoCollInputError,
            GoCollTrustError,
        ),
    ):
        return int(A2AErrorCode.INVALID_INPUT)
    if isinstance(exc, (AuthenticationError, AuthorizationError, PermissionError)):
        return int(A2AErrorCode.AUTHORIZATION)
    if isinstance(exc, (ValueError, TypeError, KeyError, FileNotFoundError)):
        return int(A2AErrorCode.INVALID_INPUT)
    return int(A2AErrorCode.INTERNAL)


def _friendly_workflow_error(errors: Any) -> str:
    """Return a safe user-facing message for workflow-level failures."""
    error_text = " ".join(str(error) for error in (errors or [])).lower()
    if any(
        marker in error_text
        for marker in ("401", "403", "access denied", "subscription key", "authentication")
    ):
        return (
            "We couldn't process the uploaded files because the document-processing "
            "service is temporarily unavailable. Please try again later or contact support."
        )
    if errors:
        return (
            "GOCOLL could not complete processing for the uploaded files. "
            "Please check the files and try again."
        )
    return "GOCOLL could not complete processing. Please try again or contact support."


# -- progress channel helpers (§8.2) ----------------------------------------
# Apply a ReasoningEvent to the stepper and push the result via EventService.
async def _apply_and_emit(events: Any, stepper: StepperState, event: ReasoningEvent) -> None:
    stepper.apply(event)
    if events is None:
        return
    # Emit the ordinary WORKING status once. Subsequent progress is carried by
    # the shared agent-reasoning flush, avoiding a stream of CUSTOM status
    # events while preserving the initial task feedback.
    if (
        event.message
        and hasattr(events, "working")
        and not getattr(events, "_gocoll_working_sent", False)
    ):
        await events.working(event.message)
        events._gocoll_working_sent = True
    if hasattr(events, "set_steps"):
        await events.set_steps(stepper.snapshot())
    # The shared EventService owns the buffered Agent Reasoning form trace.
    # Keep this call optional so the domain logic remains compatible with an
    # older common package during rolling deployments.
    if hasattr(events, "reasoning"):
        await events.reasoning(event, terminal=stepper.is_complete)
    # EventQueue enqueue operations may complete without yielding control.
    # Give the streaming consumer a turn before the next blocking phase starts.
    await asyncio.sleep(0)


async def _emit_reasoning_progress(events: Any, message: str) -> None:
    """Emit immediate working and reasoning progress without changing the stepper."""
    if events is None:
        return
    event = ReasoningEvent(message=message, phase=ReasoningPhase.ACT)
    if hasattr(events, "working"):
        await events.working(message)
    if hasattr(events, "reasoning"):
        await events.reasoning(event)
    # Flush this phase to the A2A stream before download/validation starts.
    await asyncio.sleep(0)


async def _mark_stepper_failed(events: Any, stepper: StepperState) -> None:
    """Mark the current stage red without allowing progress I/O to mask the failure."""
    if not stepper.has_error:
        stepper.fail()
    if events is None or not hasattr(events, "set_steps"):
        return
    try:
        await events.set_steps(stepper.snapshot())
    except Exception:  # noqa: BLE001 - the original pipeline failure must win
        logger.warning("Failed to emit GOCOLL error stepper snapshot", exc_info=True)


async def _keepalive(events: Any, message: str, interval: float = 20.0) -> None:
    """Emit working heartbeats until cancelled, prevents SSE proxy idle-timeout."""
    while True:
        await asyncio.sleep(interval)
        if events is not None and hasattr(events, "working"):
            try:
                await events.working(message)
            except Exception:  # noqa: BLE001
                pass


# -- lazy collaborator wrappers ---------------------------------------------
# Kept as module-level indirections so (a) this module imports against common
# builds that predate these symbols, (b) the workflow's heavy deps load only at
# call time, and (c) unit tests can monkeypatch each collaborator by name.
def _validate_documents(paths: list[str]):
    from ms_duke_je_common.governance import validate_documents

    return validate_documents(paths)


def _coerce_period_date(value: Any):
    from ms_duke_je_common.extraction import coerce_period_date

    return coerce_period_date(value)


def _record_common_llm_tokens(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    tenant_id: str = "",
    agent_id: str = "gocoll-je-agent",
    operation: str = "vision",
) -> None:
    from ms_duke_je_common.observability import record_llm_tokens

    record_llm_tokens(
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        tenant_id=tenant_id,
        agent_id=agent_id,
        operation=operation,
    )


async def _run_gocoll_workflow(**kwargs: Any) -> dict:
    from src.workflow.gocoll_workflow import run_gocoll_workflow

    return await run_gocoll_workflow(**kwargs)


def _cleanup_temp_artifacts(paths: list[str]) -> None:
    """Best-effort removal of the per-run inputs this agent materialized.

    Covers the `mkdtemp` directories created for inline base64 payloads and the
    File-Manager download-cache files fetched while resolving this request's
    inputs. Without it a long-lived pod accumulates `gocoll-agent-*` temp dirs
    and `<upload_dir>/fm/*` cache files for every run. Never raises - a cleanup
    failure must not mask the run outcome - and only artifacts this run created
    are passed in, so pass-through inputs the caller already had locally (and the
    shared cache directories themselves) are left untouched.
    """
    for path in paths:
        if not path:
            continue
        try:
            target = Path(path)
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
        except Exception:  # noqa: BLE001 - cleanup is best-effort
            logger.debug("GOCOLL: temp cleanup skipped for %s", path, exc_info=True)


class GoCollAgentLogic(BaseAgentLogic):
    """Domain logic: materialize -> trust -> period -> workflow -> enrich."""

    async def on_startup(self, services: Any = None) -> None:
        """Register bundled GOCOLL configs into the central registry (§9.2).

        Best-effort: a not-yet-ready registry degrades to a warning (configs then
        load lazily on the first pipeline run) rather than blocking boot - parity
        with the legacy `_try_preload_configs` pre-start hook.
        """
        try:
            from ms_duke_je_common.config.register import register_configs

            config_dir = str(Path(__file__).resolve().parents[2] / "config")
            await register_configs(
                service_id="ms-duke-je-gocoll-agent-service",
                config_dir=config_dir,
                category="agent",
            )
            logger.info("GOCOLL config registration complete")
        except Exception:  # noqa: BLE001 - registry unavailable -> lazy load later
            logger.warning(
                "GOCOLL config preload failed - registry not yet available; "
                "configs will load on the first pipeline run.",
                exc_info=True,
            )

    async def execute_business_logic(self, ctx: ExecutionContext) -> dict:
        started = time.monotonic()
        inputs = parse_inputs(ctx.request)
        # TEMP diagnostic (WARNING so it survives INFO filtering): show exactly what
        # the framework delivered, so we can tell a genuinely empty request (~ CM
        # recovery) from a parse miss (input present but has_input=False).
        _id_data = getattr(ctx.request, "input_data", None) or {}
        logger.warning(
            "GOCOLL input shape: has_input=%s dp=%s data_parts=%d input_files=%d "
            "output_files=%d src_files=%d wb=%s b64=%d conv_id=%s dp_keys=%s",
            "present" if inputs.dp is not None else "None",
            len(_id_data.get("data_parts") or []),
            len(_id_data.get("input_files") or []),
            len(inputs.source_files),
            "yes" if inputs.workbook_path else "no",
            len(inputs.source_file_contents),
            inputs.conversation_id or "EMPTY",
            ",".join(
                sorted(
                    {
                        str(k)
                        for d in (_id_data.get("data_parts") or [])
                        if isinstance(d, dict)
                        for k in d.keys()
                    }
                )
            )[:200],
        )
        tenant_id = inputs.tenant_id
        obs.set_span_tenant(tenant_id, task_id=ctx.request.execution_id)

        events = getattr(ctx.services, "events", None)
        logger.info("GOCOLL progress channel: events=%s sink=%s",
                    type(events).__name__ if events is not None else None,
                    getattr(events, "sink", "N/A"))
        stepper = StepperState(_STAGE_DEFS)

        async def _on_stage(
            node: str,
            status: str,
            message: str,
            detail: str = "",
        ) -> None:
            await _apply_and_emit(events, stepper, ReasoningEvent(
                message=message,
                detail=detail,
                phase=ReasoningPhase.ACT,
                flow_node=node,
                flow_status=FlowStatus(status),
            ))
            logger.info(
                "GOCOLL progress event emitted: node=%s status=%s "
                "request_elapsed=%.1fs message=%s",
                node,
                status,
                time.monotonic() - started,
                message,
            )

        await _apply_and_emit(events, stepper, ReasoningEvent(
            message="Processing GOCOLL Journal Entry...",
            phase=ReasoningPhase.THINK,
            flow_node="upload",
            flow_status=FlowStatus.ACTIVE,
        ))

        if not inputs.has_input:
            # Input-response continuation: a turn can arrive with no inline parts
            # because the files were uploaded on a prior turn and live in the
            # conversation history. Recover them through the governed memory
            # service (§8.4) - one policy/tracing/auth path, no raw egress - before
            # failing. Still no run is recorded if nothing turns up, preserving the
            # legacy early-exit before record_run.
            memory = getattr(ctx.services, "memory", None)
            conv_files: list[str] = []
            if memory is not None:
                try:
                    attachments = await memory.get_conversation_files()
                    conv_files = [a.url for a in attachments]
                except Exception as exc:  # noqa: BLE001 - CM optional; degrade to no files
                    logger.warning(
                        "GOCOLL: conversation-history recovery failed: %s", exc
                    )
            else:
                logger.warning(
                    "GOCOLL: memory service not enabled - cannot recover input files "
                    "from conversation history for conv_id=%s",
                    inputs.conversation_id or "EMPTY",
                )
            if not conv_files:
                await _mark_stepper_failed(events, stepper)
                raise GoCollInputError(
                    "No valid GOCOLL input found. Expected batch PDFs + Validation "
                    "Tab / Treasury / WF reports as file parts or a DataPart with "
                    "source_files, workbook_path, or source_file_contents."
                )
            inputs.input_files = [
                {
                    "url": u, "name": u.rstrip("/").split("/")[-1], "io": "input"
                }
                for u in conv_files
            ]
            logger.info(
                "GOCOLL: recovered %d input file(s) from conversation history",
                len(conv_files),
            )

        # Start the heartbeat before materialization and trust validation too.
        # Those operations can be silent long enough for the SSE proxy to
        # consider the stream idle.
        _cleanup_paths: list[str] = []
        _hb = (
            asyncio.create_task(_keepalive(events, "Processing GOCOLL Journal Entry..."))
            if events is not None and hasattr(events, "working")
            else None
        )
        try:
            await _emit_reasoning_progress(events, "Downloading uploaded files...")
            source_paths = await self._materialize(ctx, inputs, _cleanup_paths)
            logger.info("GOCOLL: resolved %d source file(s)", len(source_paths))

            await _emit_reasoning_progress(events, "Validating source files...")
            trust_results = await asyncio.to_thread(
                self._trust_gate,
                source_paths,
                tenant_id,
            )

            await _apply_and_emit(events, stepper, ReasoningEvent(
                message="Extracting transactions from batch PDFs...",
                phase=ReasoningPhase.ACT,
                flow_node="extraction",
                flow_status=FlowStatus.ACTIVE,
            ))

            # Governance: per-run LLM cost tracker + rate limiter (matches the
            # other domain agents). Instantiated per request; budget is per-run.
            budget_summary = get_agent_llm_budget_summary("GOCOLL")
            cost_tracker = CostTracker(rate_limiter=create_rate_limiter_for_agent("GOCOLL"))

            _pd_coerced = _coerce_period_date(inputs.period_date)
            _pd_raw = _pd_coerced.isoformat() if _pd_coerced is not None else None

            # Preserve the canonical FilePart URL against the exact materialized
            # path. Triage classifies those paths by filename/content and assigns
            # the URL to Validation/WF/Treasury without relying on list position
            # or filename keywords (combined workbooks can satisfy all three).
            _fm_url_by_path: dict[str, str] = {}
            for _sp, _f in zip(source_paths, inputs.input_files):
                _url = str(_f.get("url") or "")
                if _url:
                    _fm_url_by_path[str(Path(_sp).resolve())] = _url
            logger.info("GOCOLL retained %d input FM URL(s)", len(_fm_url_by_path))

            # Both vision service and progress emitter ride request-scoped
            # contextvars so the workflow DAG nodes can read them back (§8.5/C6).
            with use_vision_service(getattr(ctx.services, "vision", None)), \
                    use_progress_emitter(_on_stage):
                wf_result = await _run_gocoll_workflow(
                    file_paths=source_paths,
                    user_id=inputs.user_id,
                    period_date=_pd_raw,
                    period_label=inputs.period_label,
                    run_id=inputs.run_id,
                    go_form_paths=inputs.go_form_paths,
                    fm_url_by_path=_fm_url_by_path,
                )

            self._enrich(
                wf_result, cost_tracker, budget_summary, trust_results, inputs.run_id, tenant_id
            )
            # Output enrichment: expose resolved source files + FM output URL for downstream steps.
            wf_result["source_files"] = list(source_paths)
            if inputs.fm_output_url:
                wf_result["fm_output_url"] = inputs.fm_output_url

            if wf_result.get("status") == "VERIFIED":
                try:
                    envelope = self._build_envelope(wf_result)
                    wf_result["pipeline_envelope"] = envelope.model_dump()
                except Exception:
                    logger.warning("Failed to build PipelineResultEnvelope", exc_info=True)

            status = wf_result.get("status", "UNKNOWN")
            if status != PipelineStatus.VERIFIED:
                await _mark_stepper_failed(events, stepper)
            else:
                # GOCOLL is a leaf agent in standalone deployments. A verified
                # GOCOLL task hands its result to the caller for ERP rendering;
                # it must therefore leave ERP active rather than allowing the
                # stepper's next unfinished stage (Complete) to appear active.
                await _apply_and_emit(events, stepper, ReasoningEvent(
                    message="GOCOLL processing complete; handing off to ERP output...",
                    phase=ReasoningPhase.ACT,
                    flow_node="erp",
                    flow_status=FlowStatus.ACTIVE,
                ))

            self._set_response(ctx, wf_result, status)

            _duration_s = time.monotonic() - started
            obs.record_run(status=status, tenant_id=tenant_id)
            record_pipeline_duration(_duration_s, tenant_id=tenant_id, duration_s=_duration_s)
            import json as _json
            logger.info("GOCOLL wf_result keys: %s", list(wf_result.keys()))
            _bo = wf_result.get("_build_output") or {}
            logger.info("GOCOLL _build_output keys/sizes: %s",
                        {k: len(_json.dumps(v, default=str)) for k, v in _bo.items()})
            return wf_result

        except GoCollTrustError:
            # Document rejected at the trust gate: record the halt, then re-raise
            # (the framework maps it to INVALID_INPUT and emits FAILED).
            await _mark_stepper_failed(events, stepper)
            obs.record_run(status=PipelineStatus.HALTED, tenant_id=tenant_id)
            raise
        except Exception:
            await _mark_stepper_failed(events, stepper)
            _duration_s = time.monotonic() - started
            obs.record_run(
                status=PipelineStatus.FAILED, tenant_id=tenant_id, duration_s=_duration_s
            )
            record_pipeline_duration(
                _duration_s, usecase_id="gocoll", status=PipelineStatus.FAILED
            )
            raise
        finally:
            if _hb is not None:
                _hb.cancel()
                await asyncio.gather(_hb, return_exceptions=True)
            # Reclaim the per-run inputs materialized above (base64 temp dirs and
            # downloaded FM cache files); a long-lived pod would otherwise leak
            # them for every run. Best-effort - never masks the run outcome.
            _cleanup_temp_artifacts(_cleanup_paths)

    # -- response envelope hints (§8.1/8.3) ---------------------------------
    def _set_response(self, ctx: ExecutionContext, wf_result: dict, status: str) -> None:
        """Populate the response bag the executor turns into the A2A envelope.

        `ui_kind=result` emits the workflow dict as a native `uiKind=result`
        DataPart (numbers stay numbers). `success` selects the terminal state
        (COMPLETED iff VERIFIED, else FAILED - the result + aiMetrics are still
        emitted). Token counts/cost land on `llm_usage` and the model id on
        `ai_metrics` so the executor derives the terminal `aiMetrics`.
        """
        user_result = wf_result
        # The ERP step is the final producer of the UI response. Keep the
        # canonical envelope fields available at the top level of the
        # GOCOLL response so a downstream ERP agent can enrich this same
        # payload with output_file/output_fileId instead of having to depend
        # on the Router to merge two responses.
        envelope = wf_result.get("pipeline_envelope")
        if isinstance(envelope, dict):
            user_result = dict(wf_result)
            for key in (
                "envelope_version",
                "pipeline_status",
                "summary",
                "metrics",
                "line_items",
                "validation",
                "calculation_logic",
                "ai_metadata",
                "explainability_report",
            ):
                if key in envelope:
                    user_result[key] = envelope[key]
            # The envelope is intentionally sparse on artifacts before ERP
            # publishes the workbook. Preserve any workflow artifacts until
            # ERP appends the final ERP artifact.
            if envelope.get("artifacts"):
                user_result["artifacts"] = envelope["artifacts"]
            for key, value in (envelope.get("metrics") or {}).items():
                user_result.setdefault(key, value)

        build_output = wf_result.get("_build_output") or {}
        if isinstance(build_output, dict) and build_output.get("erp_type"):
            if user_result is wf_result:
                user_result = dict(user_result)
            user_result.setdefault("erp_type", build_output["erp_type"])

        workflow_errors = wf_result.get("errors") or []
        if workflow_errors:
            # Keep technical details in server-side workflow logs, but do not
            # expose provider URLs, status codes, or credential diagnostics in
            # the result artifact returned to the user.
            user_result = dict(wf_result)
            user_result["errors"] = ["Document processing could not be completed."]

        ctx.response.result = user_result
        ctx.response.ui_kind = "result"
        ctx.response.success = status == PipelineStatus.VERIFIED

        if workflow_errors:
            ctx.response.status_message = _friendly_workflow_error(workflow_errors)
        else:
            ctx.response.status_message = (
                f"GOCOLL Workflow complete. Status: {status}. "
                f"Artifacts: {len(wf_result.get('artifacts', []))}."
            )

        _usage = (wf_result.get("summary") or {}).get("llm_usage") or {}
        _pt = int(_usage.get("prompt_tokens", 0) or 0)
        _ct = int(_usage.get("completion_tokens", 0) or 0)
        _model = str(_usage.get("model") or "") or "gpt-5.2"
        usage = ctx.response.llm_usage
        if usage is not None:
            usage.prompt_tokens = _pt
            usage.completion_tokens = _ct
            usage.total_tokens = _pt + _ct
            usage.estimated_cost_usd = float(
                (wf_result.get("cost_summary") or {}).get("estimated_cost_usd", 0.0)
            )
        ctx.response.ai_metrics = {"modelId": _model}

    # -- steps --------------------------------------------------------------
    async def _materialize(
        self,
        ctx: ExecutionContext,
        inputs: GoCollInputs,
        cleanup: list[str] | None = None,
    ) -> list[str]:
        """Resolve every referenced document to a local path via the file service.

        Uses the governed `FileService.ensure_local` (§8.4). Falls back to the
        storage backend directly if no file service is wired (e.g. the storage
        provider failed to initialize), preserving materialization rather than
        silently dropping blob/FM references.

        Any per-run artifacts created here - base64 `mkdtemp` dirs and freshly
        downloaded FM cache files - are appended to `cleanup` (when provided) so
        the caller removes them in its `finally`. Pass-through paths the caller
        already had locally keep their original ref and are deliberately excluded.
        """
        files = ctx.services.files
        if files is None or not hasattr(files, "ensure_local"):
            from ms_duke_je_common.storage import get_storage

            files = get_storage()
        if inputs.dp is not None:
            raw_paths: list[str] = list(inputs.source_files)
            if inputs.workbook_path:
                raw_paths.insert(0, inputs.workbook_path)
            source_paths = await resolve_source_paths(files, raw_paths)
            if cleanup is not None:
                # resolve_source_paths is 1:1 and order-preserving; a path that
                # changed was downloaded (a pass-through keeps its original ref).
                cleanup.extend(
                    out for raw, out in zip(raw_paths, source_paths) if out != raw
                )
            # base64 fallback: materialize files that couldn't be stored as paths
            b64_paths = materialize_base64(inputs.source_file_contents)
            source_paths.extend(b64_paths)
            if cleanup is not None:
                # each base64 payload lands in its own mkdtemp dir - drop the dir
                cleanup.extend(str(Path(p).parent) for p in b64_paths)
            return source_paths
        source_paths = await resolve_input_file_parts(files, inputs.input_files)
        if cleanup is not None:
            orig_urls = (str(f or {}).get("url") or "" for f in inputs.input_files)
            cleanup.extend(p for p in source_paths if p not in orig_urls)
        return source_paths

    def _trust_gate(self, source_paths: list[str], tenant_id: str) -> list[Any]:
        """RULE 41/43: trust-validate every resolved document before any parser.

        Checks size ceiling, magic-byte/extension agreement, and a streamed
        content hash. Emits the rejection SLI and raises on any violation.
        """
        trust_results, trust_violations = _validate_documents(source_paths)
        if trust_violations:
            obs.record_document_rejected(
                len(trust_violations), tenant_id=tenant_id, reason="trust_validation"
            )
            raise GoCollTrustError(
                "Source document rejected: " + "; ".join(trust_violations)
            )
        obs.record_documents_ingested(
            len(trust_results),
            tenant_id=tenant_id,
            total_bytes=sum(r.size_bytes for r in trust_results),
        )
        if any(r.requires_async for r in trust_results):
            logger.info(
                "GOCOLL: %d document(s) exceed the async threshold - run is "
                "already executing as an A2A task",
                sum(1 for r in trust_results if r.requires_async),
            )
        return trust_results

    # -- PipelineResultEnvelope assembly ------------------------------------
    def _build_envelope(self, wf_result: dict) -> PipelineResultEnvelope:
        """Build the canonical envelope from the raw workflow output."""
        _go_build = wf_result.get("_build_output") or {}
        _go_summary = wf_result.get("summary") or {}

        def _to_amount(v: Any) -> float:
            try:
                return float(str(v).replace(",", "").strip() or 0)
            except (TypeError, ValueError):
                return 0.0

        efis_rows: list[dict] = _go_build.get("efis_rows") or []
        if not isinstance(efis_rows, list):
            efis_rows = []

        line_count = int(_go_build.get("line_count") or _go_summary.get("line_count") or len(efis_rows))
        period_label = _go_summary.get("period_label") or _go_build.get("period_label") or ""
        journal_date = _go_summary.get("journal_date") or _go_build.get("journal_date") or ""
        batch_count = int(_go_summary.get("batch_count") or len(_go_build.get("batches") or []))
        txn_count = int(_go_summary.get("transaction_count") or 0)
        balanced = bool(_go_summary.get("balanced", _go_build.get("balanced", False)))
        monetary_net = _to_amount(_go_summary.get("monetary_total") or _go_build.get("monetary_total"))
        amounts = [_to_amount(r.get("I_monetary_amount")) for r in efis_rows if isinstance(r, dict)]
        total_debits = round(sum(a for a in amounts if a > 0), 2)
        total_credits = round(sum(-a for a in amounts if a < 0), 2)

        line_items = [
            {
                "line_seq": idx + 1,
                "account": r.get("K_account"),
                "amount": _to_amount(r.get("I_monetary_amount")),
                "monetary_amount": _to_amount(r.get("I_monetary_amount")),
                "business_unit": r.get("O_line_bus_unit"),
                "operating_unit": r.get("M_oper_unit"),
                "department": r.get("M_resp_center"),
                "affiliate": r.get("L_affiliate"),
                "product": r.get("S_product"),
                "resource_type": r.get("L_resource_type"),
                "description": r.get("W_line_descr"),
                "je_type": "STATISTICAL" if r.get("AE_statistics_cd") else "MONETARY",
            }
            for idx, r in enumerate(efis_rows)
            if isinstance(r, dict)
        ]

        checks = [
            ValidationCheck(
                name=str(c.get("name") or c.get("check_name") or ""),
                status="PASS" if c.get("passed", True) else "FAIL",
                detail=str(c.get("detail") or c.get("details") or ""),
            )
            for c in (_go_summary.get("checks") or [])
            if isinstance(c, dict)
        ]

        cost_summary = wf_result.get("cost_summary") or {}
        return PipelineResultEnvelope(
            status="VERIFIED",
            summary=(
                f"VERIFIED - {line_count} eFIS line(s) across {batch_count} batch(es), "
                f"${total_debits:.2f} debits / ${total_credits:.2f} credits"
                + (f" ({period_label})" if period_label else "")
            ),
            metrics={
                "line_count": line_count,
                "monetary_total": monetary_net,
                "total_debits": total_debits,
                "total_credits": total_credits,
                "period_label": period_label,
                "journal_date": journal_date,
                "batch_count": batch_count,
                "transaction_count": txn_count,
                "efis_row_count": len(efis_rows),
                "balanced": balanced,
                "llm_tokens_used": int(cost_summary.get("total_tokens", 0) or 0),
                "llm_cost_usd": float(cost_summary.get("estimated_cost_usd", 0.0) or 0.0),
            },
            line_items=line_items,
            validation=PipelineValidation(
                overall="VERIFIED",
                checks=checks,
                failure_reasons=list(_go_summary.get("failure_reasons") or []),
            ),
            calculation_logic=dict(_go_build.get("calculation_logic") or {}),
            explainability_report=_go_build.get("explainability_report"),
            raw={
                "erp_type": "gocoll",
                "cost_summary": cost_summary,
                "llm_budget": wf_result.get("llm_budget") or {},
            },
        )

    def _enrich(
        self,
        wf_result: dict,
        cost_tracker: Any,
        budget_summary: Any,
        trust_results: list[Any],
        run_id: str,
        tenant_id: str,
    ) -> None:
        """Record the workflow's LLM usage and attach cost/budget/hash summaries."""
        _usage = (wf_result.get("summary") or {}).get("llm_usage") or {}
        _pt = int(_usage.get("prompt_tokens", 0) or 0)
        _ct = int(_usage.get("completion_tokens", 0) or 0)
        _model = str(_usage.get("model") or "") or "gpt-5.2"
        if _pt or _ct:
            try:
                cost_tracker.record(run_id, AGENT_NAME, _model, _pt, _ct, "vision_extraction")
            except RateLimitExceededError as rle:
                # Work is already complete; surface the breach for governance
                # visibility rather than discarding the finished run.
                logger.warning("GOCOLL LLM budget exceeded: %s", rle)
        wf_result["cost_summary"] = cost_tracker.get_summary(run_id)
        wf_result["llm_budget"] = budget_summary
        wf_result["source_document_hashes"] = {r.path.name: r.sha256 for r in trust_results}
        obs.record_llm_tokens(
            prompt_tokens=_pt, completion_tokens=_ct, model=_model, tenant_id=tenant_id
        )
        _record_common_llm_tokens(
            model=_model, prompt_tokens=_pt, completion_tokens=_ct, total_tokens=_pt + _ct,
            tenant_id=tenant_id,
        )