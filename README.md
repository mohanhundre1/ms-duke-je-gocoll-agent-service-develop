# ms-duke-je-gocoll-agent-service

> **Duke Energy MJE Automation — CORP GOCOLL JE Agent Service**
>
> Container hosting the agent that assembles the **GO Collection (GOCOLL)** lockbox journal entry for Duke Energy. It extracts lockbox deposit transactions and GO-Collection-Form accounting code blocks from batch PDFs, classifies and assembles balanced eFIS journal entries, and reconciles batch totals to the Treasury / Wells Fargo lockbox reports.

---

## Agent in this container

| Agent | Default Port | Responsibility |
|---|---|---|
| **GOCOLL JE Agent** | `8018` | 4-engine pipeline: Extraction → Classification → JE Assembly → Validation & Reconciliation |

### Pipeline Engines

The GOCOLL agent runs four sequential engines per execution:

1. **Extraction Engine** — Parses batch-PDF transaction summaries (text layer) and uses GPT Vision to read GO-Collection-Form / check code blocks from image-only pages
2. **Classification & Assembly Engine** — Transcribes accounting code blocks as read and builds the cash control line that nets each batch to zero; missing or rejected values are flagged for analyst follow-up rather than replaced automatically
3. **JE Assembly Engine** — Constructs the eFIS journal entry line items (Sheet1 schema)
4. **Validation & Reconciliation Engine** — Validates dimensions against the master code-block reference, enforces balance = 0, and reconciles batch totals to the WF lockbox report (BAI 115 / 566, OTC)

---

## Repository Structure

```text
ms-duke-je-gocoll-agent-service/
├── agent/                  # GOCOLL JE Agent
│   ├── src/                # Agent source code
│   │   ├── agents/         # Agent class definitions
│   │   ├── assembly/       # JE assembly logic (eFIS formatter)
│   │   ├── models/         # Pydantic data models
│   │   ├── source_parsers/ # PDF text + GPT vision extraction
│   │   ├── pipeline.py     # 4-engine pipeline orchestration
│   │   └── validation/     # JE validation + reconciliation rules
│   ├── config/             # YAML configuration files
│   │   ├── codeblocks.yaml   # Fallback accounting code blocks
│   │   ├── reconciliation.yaml # BAI code map, lockbox/site constants
│   │   ├── validation.yaml   # Validation rules (gocoll_v1)
│   │   ├── prompts.yaml    # Vision prompt templates
│   │   └── excel_format.yaml # Output eFIS Sheet1 formatting
├── serve.py                # Executor launcher
├── requirements.txt        # Python dependencies (pinned)
├── Dockerfile              # Single-stage Python 3.13-slim image
└── docker-compose.yml      # Standalone compose for local dev/testing