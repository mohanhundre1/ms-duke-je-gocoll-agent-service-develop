"""Local GOCOLL pipeline runner (dev convenience - not shipped in the image).

Runs the full 4-engine pipeline (extraction -> classification -> assembly ->
validation/reconciliation) against local files, mirroring what the A2A workflow
does, without needing the running agents, JWT auth, or the downstream services.

Usage (from the repo root, venv active):

    python run_local.py path\\to\\batch_632.pdf [more files ...] \\
        --period "JAN2025_BATCH:632" --journal-date 2026-01-31 \\
        --json out.json

Any mix of files is auto-classified: PDFs become batch inputs, and combined /
individual workbooks are matched to the WF, Treasury and Validation-Tab feeds by
filename then sheet content. Use --go-form to pass GO-Collection-Form files
explicitly. Vision extraction runs only if Azure OpenAI is configured in .env;
otherwise the text tier is used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
AGENT_DIR = REPO_ROOT / "agent"
# The agent package imports as ``from src....`` so agent/ must be importable.
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from src.gocoll.pipeline import run_pipeline  # noqa: E402
from src.source_parsers.gocoll_inputs import classify_inputs  # noqa: E402
from src.source_parsers.treasury_parser import parse_treasury_report  # noqa: E402
from src.source_parsers.wf_report_parser import parse_wf_report  # noqa: E402
from src.validation.validation_tab_master import (  # noqa: E402
    load_validation_tab_from_workbook,
)
from src.workflow.gocoll_workflow import _build_aoai_client  # noqa: E402


def _safe_parse(label: str, path: Path | None, parser):
    if path is None:
        return None
    try:
        return parser(str(path))
    except Exception as exc:  # noqa: BLE001 - feeds are best-effort locally
        print(f"[warn] failed to parse {label} from {path}: {exc}", file=sys.stderr)
        return None


async def _run(args: argparse.Namespace) -> int:
    inputs = classify_inputs(args.files)

    if not inputs.batch_pdfs:
        print("[error] no batch PDF found among the supplied files", file=sys.stderr)
        return 2

    print("Resolved inputs:")
    print(f"  batch PDFs       : {[str(p) for p in inputs.batch_pdfs]}")
    print(f"  validation tab   : {inputs.validation_tab}")
    print(f"  treasury         : {inputs.treasury}")
    print(f"  wf report        : {inputs.wf_report}")
    if inputs.unknown:
        print(f"  unknown          : {[str(p) for p in inputs.unknown]}")

    wf_report = _safe_parse("WF report", inputs.wf_report, parse_wf_report)
    treasury_report = _safe_parse("Treasury report", inputs.treasury, parse_treasury_report)
    validation_master = _safe_parse(
        "Validation Tab", inputs.validation_tab, load_validation_tab_from_workbook
    )

    aoai_client = _build_aoai_client()
    print(f"Vision tier   : {'ENABLED' if aoai_client else 'disabled (text tier only)'}")

    result = await run_pipeline(
        inputs.batch_pdfs,
        period_label=args.period,
        journal_date=args.journal_date,
        aoai_client=aoai_client,
        model=os.environ.get("GOCOLL_VISION_MODEL", "gpt-4.1"),
        fallback_model=os.environ.get("GOCOLL_VISION_FALLBACK_MODEL", "gpt-4.1"),
        image_dpi=args.dpi,
        go_form_paths=args.go_form or None,
        wf_report=wf_report,
        treasury_report=treasury_report,
        validation_master=validation_master,
    )

    print("\n" + "=" * 60)
    print(f"STATUS: {result.status}")
    print(f"eFIS rows: {len(result.efis_rows)}")
    if result.warnings:
        print(f"warnings: {result.warnings}")
    for check in result.processing_result.checks:
        print(f"  [{check.status}] {check.check_name}")
    print("=" * 60)

    for row in result.efis_rows:
        print(row)

    if args.json:
        out = Path(args.json)
        out.write_text(
            json.dumps(
                {
                    "status": str(result.status),
                    "efis_rows": result.efis_rows,
                    "warnings": result.warnings,
                    "checks": [
                        {"name": c.check_name, "status": str(c.status), "details": c.details}
                        for c in result.processing_result.checks
                    ],
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"\nWrote {out}")

    return 0 if str(result.status).upper().endswith("VERIFIED") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the GOCOLL pipeline against local files.")
    parser.add_argument("files", nargs="+", help="Batch PDF(s) and optional feed workbooks.")
    parser.add_argument("--period", default="", help="eFIS period label, e.g. JAN2025_BATCH:632.")
    parser.add_argument("--journal-date", default="", help="eFIS journal date, e.g. 2026-01-31.")
    parser.add_argument("--go-form", action="append", help="Explicit GO-Collection-form file(s).")
    parser.add_argument("--dpi", type=int, default=300, help="Page render DPI for vision.")
    parser.add_argument("--json", help="Write the result JSON to this path.")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())