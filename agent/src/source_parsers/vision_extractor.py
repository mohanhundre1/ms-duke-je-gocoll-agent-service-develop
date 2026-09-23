"""GPT Vision Extraction - uses Azure OpenAI GPT vision to extract
structured data from PDF page images.

The extraction implementation lives in the shared module at
``ms-duke-je-shared-agent-service/vision``. This file loads the GOCOLL
prompt registry (``_PROMPTS``) from ``agent/config/vision_prompts/*.txt``
and provides a thin ``extract_from_pdf`` delegator.

Supported PDF types:
    - gocoll_go_form: unified per-page read - G1 coding grid distribution lines
      plus the check identifiers (check#/batch#/sequence#/check_amount) printed
      on the page.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ms_duke_je_common.governance import PromptSanitizer

from src.source_parsers.llm_response_validation import validate_ai_extraction_output

logger = logging.getLogger(__name__)

# -- Transient-error retry (fault tolerance) -------------------------------
# The shared extractor performs a single fallback-model swap on WNet-403 /
# timeout, but does NOT retry transient rate-limit (429) or upstream 5xx
# errors, and does no backoff. For dense GOCOLL batches we fan many vision
# calls out concurrently, so brief 429 bursts are expected. These knobs add a
# bounded exponential-backoff retry around each call.
_VISION_MAX_RETRIES = max(0, int(os.getenv("GOCOLL_VISION_MAX_RETRIES", "3")))
_VISION_RETRY_BASE_S = max(0.1, float(os.getenv("GOCOLL_VISION_RETRY_BASE_S", "1.5")))

# Substrings that mark a transient error worth retrying on the SAME call.
_TRANSIENT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "too many requests",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "service unavailable",
    "503",
    "502",
    "500",
    "invalid_prompt",
    "invalid_request_error",
    "internal error",
    "unable to complete inference",
    "connection reset",
    "connection error",
    "connection aborted",
    "econnreset",
    "read timeout",
)

_DEPLOYMENT_MISSING_MARKERS = (
    "deploymentnotfound",
    "deployment for this resource does not exist",
    "404",
)

def _is_transient_vision_error(error_str: str | None) -> bool:
    """True when a failed extraction is worth a backoff retry."""
    if not error_str:
        return False
    low = error_str.lower()
    return any(marker in low for marker in _TRANSIENT_MARKERS)

def _is_missing_deployment_error(error_str: str | None) -> bool:
    """True when the configured primary deployment does not exist in Azure."""
    if not error_str:
        return False
    low = error_str.lower()
    return any(marker in low for marker in _DEPLOYMENT_MISSING_MARKERS)

# Log the full structured data returned by each vision call so the extracted
# GO-form / WF / check fields can be reviewed page-by-page. Enabled by default;
# set GOCOLL_LOG_VISION_RESULTS=0 to silence. Truncated to keep logs bounded.
_LOG_VISION_RESULTS = os.getenv("GOCOLL_LOG_VISION_RESULTS", "1").lower() not in (
    "0", "false", "no", "off",
)
_VISION_LOG_MAX_CHARS = max(500, int(os.getenv("GOCOLL_VISION_LOG_MAX_CHARS", "8000")))

def _log_vision_result(
    pdf_type: str,
    image_paths: list[Path],
    data: Any,
    success: bool,
    error: str | None = None,
) -> None:
    """Emit the extracted vision payload (lines/fields) for a single call."""
    if not _LOG_VISION_RESULTS:
        return
    try:
        import json

        pages = ",".join(Path(p).stem for p in image_paths) or "?"
        payload = json.dumps(data or {}, default=str, ensure_ascii=False)
        if len(payload) > _VISION_LOG_MAX_CHARS:
            payload = payload[:_VISION_LOG_MAX_CHARS] + " ...<truncated>"
        logger.info(
            "VISION RESULT [%s] pages=[%s] success=%s%s: %s",
            pdf_type,
            pages,
            success,
            f" error={error}" if error else "",
            payload,
        )
    except Exception:  # noqa: BLE001 - logging must never break extraction
        logger.debug("failed to log vision result", exc_info=True)

@dataclass
class ExtractionResult:
    """Result from GPT vision extraction."""
    pdf_type: str
    data: dict[str, Any] = field(default_factory=dict)
    confidence: dict[str, float] = field(default_factory=dict)
    raw_response: str = ""
    success: bool = True
    error: str | None = None
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0

# -- Prompt registry --------------------------------------------------------
# GOCOLL vision prompts live as text files under `agent/config/vision_prompts`
# (one `<pdf_type>.txt` per supported page type) so prompt copy can be tuned
# without touching code. Keys are the file stems (the `pdf_type` values).
_VISION_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "vision_prompts"

_PROMPT_SANITIZER = PromptSanitizer()

def sanitize_prompt_text(pdf_type: str, text: str) -> str:
    """Run a prompt through the shared sanitizer before it reaches the model.

    Prompt copy is tunable at runtime through the config registry, so it is not
    fully trusted input: injection markers are neutralised and PII is redacted
    before the text is registered for use (ACS RULE 18, anti-pattern G3).
    """
    sanitized = _PROMPT_SANITIZER.sanitize(text)
    if sanitized.injection_detected:
        logger.warning(
            "Vision prompt '%s' contains injection signals %s - neutralised",
            pdf_type, ",".join(sanitized.injection_signals),
        )
    if sanitized.pii_redacted:
        logger.warning("Vision prompt '%s' contained PII - redacted", pdf_type)
    return sanitized.text

def _load_prompts() -> dict[str, str]:
    """Load `<pdf_type>.txt` vision prompts from `config/vision_prompts`."""
    prompts: dict[str, str] = {}
    if _VISION_PROMPTS_DIR.is_dir():
        for path in sorted(_VISION_PROMPTS_DIR.glob("*.txt")):
            try:
                prompts[path.stem] = sanitize_prompt_text(
                    path.stem, path.read_text(encoding="utf-8")
                )
            except OSError:
                logger.warning("Failed to read vision prompt %s", path)
    if not prompts:
        logger.warning("No vision prompts found in %s", _VISION_PROMPTS_DIR)
    return prompts

_PROMPTS: dict[str, str] = _load_prompts()

# Dense GO-Collection-Form code blocks (e.g. batch-633 seq-1 carries ~29 coded
# distribution lines) take well over the shared default 32 s per-call budget
# (30 s base + 2 s/image) for the model to transcribe in full. When the call
# times out, the retry falls to the second-opinion model - which on these dense
# grids returns no usable lines - so the whole form collapses to a single
# text-tier fallback line and the per-form distribution detail is lost. Give the
# GOCOLL vision calls a generous per-call timeout so the full grid is read.
_GOCOLL_VISION_TIMEOUT_BASE_S = 120.0

# Dense GO-form grids (WESCO ~30 rows) plus higher-res tiling overflow the shared
# 4096-token output budget and truncate the JSON, silently dropping lines. Give
# the GOCOLL vision calls a much larger output budget (override, GOCOLL-scoped).
_GOCOLL_VISION_MAX_TOKENS = max(4096, int(os.getenv("GOCOLL_VISION_MAX_TOKENS", "16384")))

def _gocoll_vision_config():
    """Build the shared :class:`VisionConfig` with GOCOLL-tuned call timeout and
    output-token budget.

    Cached after first construction. Returns ``None`` if the shared config
    loader is unavailable, in which case the caller falls back to shared
    defaults.
    """
    global _GOCOLL_VISION_CONFIG
    try:
        return _GOCOLL_VISION_CONFIG
    except NameError:
        pass
    try:
        from ms_duke_je_common.vision.config_loader import load_vision_config

        cfg = load_vision_config()
        cfg.request_timeout_base_s = max(
            cfg.request_timeout_base_s, _GOCOLL_VISION_TIMEOUT_BASE_S
        )
        # max_completion_tokens exists on newer ms-duke-je-common; guard for older.
        current = getattr(cfg, "max_completion_tokens", 4096)
        try:
            cfg.max_completion_tokens = max(current, _GOCOLL_VISION_MAX_TOKENS)
        except Exception:  # noqa: BLE001 - older VisionConfig without the field
            pass
    except Exception:  # noqa: BLE001 - fall back to shared defaults on any import issue
        cfg = None
    _GOCOLL_VISION_CONFIG = cfg
    return cfg

def _gocoll_vision_config_for_model(model: str | None):
    """Return GOCOLL vision config, omitting temperature for GPT-5.6 deployments."""
    cfg = _gocoll_vision_config()
    if cfg is None:
        return None
    if "gpt-5.6" not in (model or "").lower():
        return cfg
    cfg = copy.copy(cfg)
    try:
        cfg.temperature = None
    except Exception:  # noqa: BLE001 - older VisionConfig without the field
        pass
    return cfg

async def extract_from_pdf(
    image_paths: list[Path],
    pdf_type: str,
    aoai_client: None,
    model: str = "gpt-5.2",
    fallback_model: str | None = "gpt-5.2",
) -> ExtractionResult:
    """Use Azure OpenAI vision to extract structured data from PDF page images.

    Delegates to ``ms-duke-je-shared-agent-service/vision/vision_extractor.py``.

    Args:
        image_paths: List of PNG image paths (one per page).
        pdf_type: One of the supported PDF types.
        aoai_client: Azure OpenAI async client instance.
        model: Primary model deployment.
        fallback_model: Optional fallback deployment.

    Returns:
        ``ExtractionResult`` with extracted data and confidence scores.
    """
    if aoai_client is None:
        return ExtractionResult(
            pdf_type=pdf_type,
            success=False,
            error="No Azure OpenAI client configured. Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_KEY.",
        )

    # Wave 6: shared vision extractor now lives in ms-duke-je-common
    # (pip-installed). The previous sys.path / module-stashing dance is
    # gone - just import and call.
    from ms_duke_je_common.vision import vision_extractor as _shared_vision
    from ms_duke_je_common.vision.vision_extractor import (
        extract_from_pdf as shared_extract,
    )

    # The shared module owns the prompt registry that ``_get_prompt`` reads.
    # Register the GOCOLL prompt (defined locally above) into that registry so
    # ``shared_extract`` can resolve ``gocoll_go_form`` without a shared-library
    # rebuild.
    for _gocoll_type in ("gocoll_go_form",):
        if _gocoll_type in _PROMPTS:
            _shared_vision._PROMPTS.setdefault(_gocoll_type, _PROMPTS[_gocoll_type])

    shared_result = await shared_extract(
        image_paths=image_paths,
        pdf_type=pdf_type,
        aoai_client=aoai_client,
        model=model,
        fallback_model=fallback_model,
        config=_gocoll_vision_config_for_model(model),
    )

    if (
        not shared_result.success
        and fallback_model
        and fallback_model != model
        and (
            _is_missing_deployment_error(shared_result.error)
            or _is_transient_vision_error(shared_result.error)
        )
    ):
        logger.warning(
            "Vision primary %s failed for %s (%s); retrying with fallback %s",
            model, pdf_type, shared_result.error, fallback_model,
        )
        shared_result = await shared_extract(
            image_paths=image_paths,
            pdf_type=pdf_type,
            aoai_client=aoai_client,
            model=fallback_model,
            fallback_model=None,
            config=_gocoll_vision_config_for_model(fallback_model),
        )

    # Fault tolerance: retry transient failures (429 / 5xx / connection /
    # timeout) with exponential backoff + jitter. The shared extractor already
    # handles the one-shot fallback-model swap on WNet-403; this loop adds the
    # short, bounded retries that a concurrent vision fan-out needs.
    attempt = 0
    while (
        not shared_result.success
        and attempt < _VISION_MAX_RETRIES
        and _is_transient_vision_error(shared_result.error)
    ):
        attempt += 1
        delay = _VISION_RETRY_BASE_S * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
        logger.warning(
            "Vision transient error on %s (attempt %d/%d): %s - retrying in %.1fs",
            pdf_type, attempt, _VISION_MAX_RETRIES, shared_result.error, delay,
        )
        await asyncio.sleep(delay)
        shared_result = await shared_extract(
            image_paths=image_paths,
            pdf_type=pdf_type,
            aoai_client=aoai_client,
            model=model,
            fallback_model=fallback_model,
            config=_gocoll_vision_config_for_model(model),
        )

    if shared_result.model_id or shared_result.prompt_tokens:
        logger.info(
            "Shared vision: pdf_type=%s model=%s prompt_tokens=%s completion_tokens=%s",
            shared_result.pdf_type, shared_result.model_id,
            shared_result.prompt_tokens, shared_result.completion_tokens,
        )

    if shared_result.success:
        validated, validation_error = validate_ai_extraction_output(
            shared_result.pdf_type,
            shared_result.data,
        )
        if validation_error:
            _log_vision_result(
                shared_result.pdf_type, image_paths, shared_result.data, False, validation_error
            )
            return ExtractionResult(
                pdf_type=shared_result.pdf_type,
                raw_response=shared_result.raw_response,
                success=False,
                error=validation_error,
                model=shared_result.model_id,
                prompt_tokens=shared_result.prompt_tokens,
                completion_tokens=shared_result.completion_tokens,
            )
        else:
            validated = shared_result.data

        _log_vision_result(
            shared_result.pdf_type, image_paths, validated, shared_result.success,
            shared_result.error,
        )
        return ExtractionResult(
            pdf_type=shared_result.pdf_type,
            data=validated or {},
            confidence=shared_result.confidence,
            raw_response=shared_result.raw_response,
            success=shared_result.success,
            error=shared_result.error,
            model=shared_result.model_id,
            prompt_tokens=shared_result.prompt_tokens,
            completion_tokens=shared_result.completion_tokens,
        )