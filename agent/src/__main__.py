"""GOColl JE Agent - A2A server entry point.

Composition root (§10/§11): wraps :class:`GoCollAgentLogic` in the framework
governance pipeline (error-handler → telemetry → auth → rbac → output-guard →
audit → prompt-guard → cost) and serves it over A2A via ``run_pipeline_agent``.
OTel/telemetry is configured by the framework telemetry provider; the GOColl
domain SLIs live in ``src.observability`` and the logic. The agent is
discoverable at /.well-known/agent-card.json.
"""

from __future__ import annotations

import logging
import os

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))

# GOColl reads source files and writes outputs through the File Manager, not
# Azure Blob. Default the storage backend to the FM provider so the framework
# does not build AzureBlobStorageProvider (which requires blob containers).
os.environ.setdefault("STORAGE_PROVIDER", "fm")

from ms_duke_je_common.entrypoints.pipeline_server import run_pipeline_agent  # noqa: E402
from ms_duke_je_common.governance.cost_tracker import CostTracker  # noqa: E402
from ms_duke_je_common.observability import setup_json_logging  # noqa: E402

from src.agents.agent_card import create_agent_card  # noqa: E402
from src.factories.bootstrap import validate_required_env_vars  # noqa: E402
from src.logic.agent import GOCollAgentLogic, gocoll_error_code  # noqa: E402

# Hydrate secrets from provider
try:
    from ms_duke_je_common.bootstrap_secrets import hydrate_env_from_secrets  # noqa: E402
    hydrate_env_from_secrets(("INTERNAL_SERVICE_KEY", "SERVICE_BASE_URL", "OTEL_ENDPOINT"))
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

setup_json_logging()
logger = logging.getLogger(__name__)

# 200 MB - the executor receives base64-encoded source files inline, so both the
# transport content-length limit (413, §8.11) and the pipeline input ceiling
# (C4) are raised well above the framework's 10 MB default.
_MAX_BYTES = 200 * 1024 * 1024


def main() -> None:
    validate_required_env_vars()
    run_pipeline_agent(
        logic_factory=GOCollAgentLogic,
        agent_card_factory=create_agent_card,
        description="GOColl JE Agent A2A server",
        default_port=8018,
        start_banner="Starting GOColl JE Agent",
        agent_id="gocoll-je-agent",
        default_capability="gocoll",
        service_name="duke-gocoll-agent",
        # No events provider factory exists; the §8.2 progress channel is bound
        # by the executor to a bare EventService, and domain-event publishing
        # (audit) degrades to the governed logger - as in Phase 2.
        enable_services=("auth", "storage", "cache", "memory"),
        optional_middleware=("prompt_guard", "cost"),
        cost_tracker_factory=CostTracker,
        error_code_mapper=gocoll_error_code,
        max_input_bytes=_MAX_BYTES,
        max_content_length=_MAX_BYTES,
        pre_start_hooks=[validate_required_env_vars],
    )


if __name__ == "__main__":
    main()"""Placeholder for __main__.py."""
