"""Startup environment validation for the GOCOLL agent (ACS - fail fast on config)."""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)

REQUIRED_ENV_VARS: list[str] = []

#: Vars that must be present outside local/dev - the agent cannot authenticate
#: inbound A2A calls or run its vision tier without them.
PRODUCTION_REQUIRED_ENV_VARS: tuple[str, ...] = (
    "INTERNAL_SERVICE_KEY",
    "AZURE_OPENAI_ENDPOINT",
)

_LOCAL_ENVIRONMENTS = frozenset({"local", "dev", "development", "test", ""})

def _is_local() -> bool:
    env = (os.getenv("ENVIRONMENT") or os.getenv("APP_ENV") or "").strip().lower()
    return env in _LOCAL_ENVIRONMENTS


def validate_required_env_vars(required: list[str] | None = None) -> None:
    """Validate that all required environment variables are set.

    Outside local/dev, the production set is enforced as well so a
    misconfigured deployment fails at boot instead of at the first request.
    """
    vars_to_check = list(required or REQUIRED_ENV_VARS)
    if not _is_local():
        vars_to_check.extend(PRODUCTION_REQUIRED_ENV_VARS)

    missing = [var for var in dict.fromkeys(vars_to_check) if not os.getenv(var)]
    if missing:
        logger.critical("Missing required environment variables: %s", missing)
        sys.exit(1)

    if _is_local():
        soft_missing = [v for v in PRODUCTION_REQUIRED_ENV_VARS if not os.getenv(v)]
        if soft_missing:
            logger.warning(
                "Running in local mode without %s - features depending on them "
                "will be disabled",
                soft_missing,
            )