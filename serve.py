"""Executor-only launcher for the duke-gocoll container.

Starts the GOCOLL JE Agent on GOCOLL_AGENT_PORT (default 8018). The ROLE
environment variable may be unset or set to ``executor``.

The actual launcher logic lives in
`ms_duke_je_common.entrypoints.multi_launcher.run_multi_agent` (Wave 2b).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from ms_duke_je_common.bootstrap_secrets import hydrate_env_from_secrets  # noqa: E402

hydrate_env_from_secrets()

from ms_duke_je_common.entrypoints.multi_launcher import (  # noqa: E402
    RoleSpec,
    run_multi_agent,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s serve %(levelname)s %(message)s",
)

BASE = Path(os.path.abspath(__file__)).parent

ROLES = {
    "executor": RoleSpec(
        display_name="gocoll-agent",
        cwd=BASE / "agent",
        port_env="GOCOLL_AGENT_PORT",
        default_port="8018",
    ),
}

if __name__ == "__main__":
    run_multi_agent(service_name="duke-gocoll", roles=ROLES)