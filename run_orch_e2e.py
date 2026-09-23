"""End-to-end driver: GOCOLL Executor (8018) -> shared ERP Template agent (8002).

Calls the executor directly, then calls ERP with the pipeline data.

Requires:
    - shared-agent-service ERP template running (8002)
    - gocoll serve.py executor running (8018)

Input files are the mandatory GOCOLL feeds under
``DUKE\gocoll\test_data\inputs``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from ms_duke_je_common.transport.a2a_client import DownstreamAgent, extract_data_parts, extract_text_parts

INPUTS = Path(
    os.getenv(
        "GOCOLL_E2E_INPUTS",
        r"C:\Users\DS172MV\OneDrive - EY\Documents\DUKE\gocoll\test_data\inputs",
    )
)

XLSX_NAMES = [
    "Validation Tab.xlsx",
    "Treasury Report.xlsx",
    "WF Transactions Report.xlsx",
]

# Discover all batch PDFs in the inputs folder dynamically.
def _source_names() -> list[str]:
    pdfs = sorted(p.name for p in INPUTS.glob("Batch - *.pdf"))
    return pdfs + [x for x in XLSX_NAMES if (INPUTS / x).exists()]

AGENT_URL = os.getenv("GOCOLL_AGENT_URL", "http://localhost:8018")
ERP_URL = os.getenv("ERP_TEMPLATE_AGENT_URL", "http://localhost:8002")
JOB_ID = "local-e2e-001"

def _default_period_label(period_date: str) -> str:
    try:
        from datetime import datetime as _dt
        d = _dt.strptime(period_date, "%Y-%m-%d")
        return d.strftime("%b%Y").upper() + "_BATCH:augmented"
    except Exception:
        return "AUGMENTED_BATCH"

async def main() -> None:
    period_date = os.getenv("GOCOLL_E2E_PERIOD_DATE", "2026-01-31")
    period_label = os.getenv("GOCOLL_E2E_PERIOD_LABEL") or _default_period_label(period_date)

    source_files = [str(INPUTS / name) for name in _source_names()]
    missing = [p for p in source_files if not Path(p).exists()]
    if missing:
        raise SystemExit("missing input file(s):\n " + "\n ".join(missing))

    payload = {
        "source_files": source_files,
        "period_date": period_date,
        "period_label": period_label,
        "erp_type": "gocoll",
        "job_id": JOB_ID,
        "trace_id": JOB_ID,
    }
    metadata = {"job_id": JOB_ID, "trace_id": JOB_ID}

    print(f"Sending {len(source_files)} source file(s) to executor at {AGENT_URL} ...")
    for p in source_files:
        print(f"  - {Path(p).name}")
    agent = DownstreamAgent(AGENT_URL, timeout=int(os.getenv("A2A_CALL_TIMEOUT", "1800")))
    task = await agent.send_data(payload, metadata=metadata)
    print(f"Executor task state: {task.status.state}")

    data_parts = extract_data_parts(task, exclude_ui_kinds={"stepper"})
    text_parts = extract_text_parts(task)

    if not data_parts:
        print("!! No data parts in task response.")
        return

    result = next(
        (
            part
            for part in data_parts
            if part.get("build_output")
            or part.get("pipeline_envelope")
            or part.get("calculation_logic")
        ),
        data_parts[-1],
    )
    completion_text = text_parts[0] if text_parts else ""

    # Step 2: call ERP template agent - mirrors workflow generate_erp step.
    erp_result: dict = {}
    erp_output_file = ""
    _build = result.get("build_output") or {}
    if result.get("status") == "VERIFIED" and _build:
        erp_payload = {"pipeline_data": _build, "erp_type": "gocoll"}
        erp_meta = {"job_id": JOB_ID, "step": "erp_template",
                    "usecase_id": "gocoll", "period_date": period_date}
        try:
            print(f"\nCalling ERP template agent at {ERP_URL} ...")
            erp_agent = DownstreamAgent(ERP_URL, timeout=300)
            erp_task = await erp_agent.send_data(erp_payload, metadata=erp_meta)
            erp_parts = extract_data_parts(erp_task)
            erp_result = erp_parts[0] if erp_parts else {}
            erp_output_file = erp_result.get("output_file", "")
            print(f"ERP task state: {erp_task.status.state}")
        except Exception as exc:
            print(f"!! ERP call failed: {exc}")

    # Save calculation_logic and explainability_report to separate files.
    _out_dir = Path(__file__).parent / "app_output"
    _out_dir.mkdir(parents=True, exist_ok=True)
    _build = result.get("build_output") or {}
    _envelope = result.get("pipeline_envelope") or {}

    print(f"result top-level keys : {sorted(result.keys())}")
    print(f"build_output present : {bool(_build)}, keys={sorted(_build.keys())[:10] if _build else []}")
    print(f"pipeline_envelope present: {bool(_envelope)}")

    # Try all known locations: envelope -> _build_output -> top-level
    _calc = (
        _envelope.get("calculation_logic")
        or _build.get("calculation_logic")
        or result.get("calculation_logic")
    )

    _expl = (
        _envelope.get("explainability_report")
        or _build.get("explainability_report")
        or result.get("explainability_report")
    )

    if _calc is not None:
        _calc_path = _out_dir / f"calc_logic_{JOB_ID}.json"
        _calc_payload = {
            "erp_type": result.get("erp_type") or _build.get("erp_type") or "gocoll",
            "calculation_logic": _calc,
        }
        _calc_path.write_text(
            json.dumps(_calc_payload, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"calc_logic saved       : {_calc_path}")
    else:
        print("calc_logic             : not found in result")

    if _expl is not None:
        _expl_path = _out_dir / f"explainability_{JOB_ID}.json"
        _expl_path.write_text(json.dumps(_expl, indent=2, default=str), encoding="utf-8")
        print(f"explainability saved   : {_expl_path}")
    else:
        print("explainability         : not found in result")

    print("=" * 60)
    print(f"status      : {result.get('status')}")
    print(f"job_id      : {JOB_ID}")
    print(f"result.status         : {result.get('status')}")
    print(f"result.metrics        : {json.dumps(result.get('metrics'), default=str)}")
    print(f"erp_output_file       : {erp_output_file or '(none)'}")
    arts = result.get("artifacts")
    if arts:
        print(f"artifacts             : {json.dumps(arts, default=str)[:800]}")
    if completion_text:
        print(f"completion_text       : {completion_text}")
    print("=" * 60)
    print(f"Full result payload (truncated 2500 chars):")
    print(json.dumps(result, default=str, indent=2)[:2500])

if __name__ == "__main__":
    asyncio.run(main())