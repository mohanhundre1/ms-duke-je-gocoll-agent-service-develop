"""GOCOLL custom SLI metrics + tenant span attributes (ACS RULE 20 / RULE 34).

Traces, correlation IDs and log context are configured centrally by
`ms_duke_je_common.entrypoints.a2a_service.run_a2a_agent`. This module adds the
GOCOLL-specific counters/histograms that the platform HTTP metrics cannot
provide, and stamps ``tenant_id`` onto the active span so every trace and
metric can be filtered per tenant.

Every emitter degrades to a no-op when the OpenTelemetry SDK is absent, so unit
tests and local runs never need a collector.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_METER: Any = None
_INSTRUMENTS: dict[str, Any] = {}


def _get_meter() -> Any:
    global _METER
    if _METER is not None:
        return _METER or None
    try:
        from opentelemetry import metrics

        _METER = metrics.get_meter("ms-duke-je-gocoll-agent-service", "0.1.0")
    except Exception:  # noqa: BLE001 - OTEL SDK not installed/configured
        _METER = False
    return _METER or None


def _counter(name: str, description: str, unit: str = "1") -> Any:
    if name in _INSTRUMENTS:
        return _INSTRUMENTS[name]
    meter = _get_meter()
    if meter is None:
        return None
    try:
        instrument = meter.create_counter(name=name, unit=unit, description=description)
    except Exception:  # noqa: BLE001 - never break the pipeline for telemetry
        return None
    _INSTRUMENTS[name] = instrument
    return instrument


def _histogram(name: str, description: str, unit: str) -> Any:
    if name in _INSTRUMENTS:
        return _INSTRUMENTS[name]
    meter = _get_meter()
    if meter is None:
        return None
    try:
        instrument = meter.create_histogram(name=name, unit=unit, description=description)
    except Exception:  # noqa: BLE001
        return None
    _INSTRUMENTS[name] = instrument
    return instrument


def _add(instrument: Any, value: int | float, attributes: dict[str, Any]) -> None:
    if instrument is None or value is None:
        return
    try:
        instrument.add(value, attributes=attributes)
    except Exception:  # noqa: BLE001
        logger.debug("metric emit failed", exc_info=True)


def _record(instrument: Any, value: int | float, attributes: dict[str, Any]) -> None:
    if instrument is None or value is None:
        return
    try:
        instrument.record(value, attributes=attributes)
    except Exception:  # noqa: BLE001
        logger.debug("metric record failed", exc_info=True)


def _attrs(tenant_id: str = "", **extra: Any) -> dict[str, Any]:
    attrs: dict[str, Any] = {"tenant_id": tenant_id or "unknown"}
    attrs.update({k: v for k, v in extra.items() if v is not None})
    return attrs


# -- Span enrichment ---------------------------------------------------------


def set_span_tenant(tenant_id: str, **extra: Any) -> None:
    """Stamp ``tenant_id`` (plus optional ids) onto the active span."""
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span is None or not span.get_span_context().is_valid:
            return
        span.set_attribute("tenant_id", tenant_id or "unknown")
        for key, value in extra.items():
            if value is not None:
                span.set_attribute(key, str(value))
    except Exception:  # noqa: BLE001 - OTEL absent or no active span
        return


# -- Emitters ----------------------------------------------------------------


def record_documents_ingested(
    count: int, *, tenant_id: str = "", total_bytes: int = 0
) -> None:
    """Count documents accepted for processing and their aggregate size."""
    _add(
        _counter("gocoll.documents.ingested", "GOCOLL source documents accepted"),
        count,
        _attrs(tenant_id),
    )
    _add(
        _counter("gocoll.documents.bytes", "GOCOLL source document bytes", unit="By"),
        total_bytes,
        _attrs(tenant_id),
    )


def record_document_rejected(count: int, *, tenant_id: str = "", reason: str = "") -> None:
    """Count documents blocked by trust validation."""
    _add(
        _counter("gocoll.documents.rejected", "GOCOLL documents failing trust checks"),
        count,
        _attrs(tenant_id, reason=reason or "unspecified"),
    )


def record_policy_decision(*, allow: bool, tenant_id: str = "") -> None:
    """Count deterministic policy allow/deny outcomes."""
    _add(
        _counter("gocoll.policy.decisions", "GOCOLL deterministic policy decisions"),
        1,
        _attrs(tenant_id, decision="allow" if allow else "deny"),
    )


def record_run(*, status: str, tenant_id: str = "", duration_s: float | None = None) -> None:
    """Count completed runs by terminal status and record wall-clock duration."""
    _add(
        _counter("gocoll.runs.total", "GOCOLL pipeline runs by terminal status"),
        1,
        _attrs(tenant_id, status=status or "UNKNOWN"),
    )
    if duration_s is not None:
        _record(
            _histogram("gocoll.run.duration", "GOCOLL run wall-clock duration", "s"),
            duration_s,
            _attrs(tenant_id, status=status or "UNKNOWN"),
        )


def record_llm_tokens(
    *, prompt_tokens: int, completion_tokens: int, model: str = "", tenant_id: str = ""
) -> None:
    """Record vision/LLM token consumption per model (cost SLI)."""
    instrument = _counter("gocoll.llm.tokens", "GOCOLL LLM tokens consumed")
    _add(instrument, prompt_tokens, _attrs(tenant_id, model=model or "unknown", kind="prompt"))
    _add(
        instrument,
        completion_tokens,
        _attrs(tenant_id, model=model or "unknown", kind="completion"),
    )


def record_content_safety_flags(flags: set[str] | list[str], *, tenant_id: str = "") -> None:
    """Count PII/toxicity flags raised on inbound or outbound content."""
    instrument = _counter(
        "gocoll.content_safety.flags", "GOCOLL content-safety flags raised"
    )
    for flag in flags or ():
        _add(instrument, 1, _attrs(tenant_id, flag_type=str(flag)))"""Placeholder for observability.py."""
