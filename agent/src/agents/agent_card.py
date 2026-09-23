"""Agent Card definition for GOCOLL JE Agent.

Authors a transport-neutral :class:`AgentDescriptor` and renders it to an
``a2a.types.AgentCard`` inside ``ms_duke_je_common`` - so this module imports **no**
``a2a`` SDK. The zero-arg ``create_agent_card()`` factory contract consumed by
``run_pipeline_agent(agent_card_factory=...)`` is unchanged; the adapter supplies the
EY provider, JWT bearer security, the ``JSONRPC`` interface derived from
``SERVICE_BASE_URL``, and the ``application/json`` modes.
"""

from __future__ import annotations

from ms_duke_je_common import AgentDescriptor, SkillDescriptor
from ms_duke_je_common.transport import agent_card_from_descriptor

GOCOLL_DESCRIPTOR = AgentDescriptor(
    name="GOCOLL JE Agent",
    description=(
        "Processes CORP GOCOLL JE for Duke Energy: extracts lockbox deposit "
        "transactions and GO-Collection-Form accounting code blocks from batch PDFs "
        "(text + GPT vision), classifies lines with fallback code blocks, assembles "
        "balanced journal entries (detail lines + cash control line) in eFIS format, "
        "validates against the master code-block reference, and reconciles to the "
        "Treasury/WF lockbox reports."
    ),
    path_prefix="gocoll-executor",
    skills=(
        SkillDescriptor(
            id="process-gocoll-je",
            name="Process GOCOLL JE",
            description=(
                "Receives batch lockbox PDFs and reference workbook data, extracts "
                "transaction summaries and GO-Collection-Form code blocks, classifies "
                "each line (posted verbatim; lines with no GO form are flagged for "
                "analyst review), assembles a "
                "balanced eFIS journal entry (detail lines plus a cash control line that "
                "nets the batch to zero), validates dimensions against the master code "
                "reference, and reconciles batch totals to the WF lockbox report."
            ),
            tags=(
                "gocoll", "go-collections", "lockbox", "journal-entry",
                "accounting-code-extraction", "bank-reconciliation",
                "wells-fargo", "duke-energy-corp",
            ),
        ),
    ),
)

def create_agent_card():
    """Build the A2A agent discovery card (zero-arg factory contract)."""
    return agent_card_from_descriptor(GOCOLL_DESCRIPTOR)