"""GOColl extraction coordinator — Engine 1 entry point.

Ties the two extraction tiers into a single :class:`GoCollExtraction`:

1. **Text tier** (:mod:`gocoll_pdf_parser`) — reads the batch-PDF text
   layer for Transaction Summary fields (lockbox / site / deposit
   account / check# / check amount / batch#) and locates the
   GO-Collection-Form page for each transaction.
2. **Vision tier** (:mod:`vision_extractor` -> shared GPT vision) —
   transcribes the GO-Collection-Form accounting code block from the
   form image. For image-only batches (e.g. fully scanned batch 632)
   the text tier yields nothing, so the vision pass also recovers the
   check / transaction fields.

The coordinator is deliberately tolerant: a failed vision pass on one
transaction degrades that line (records a warning) rather than aborting
the batch, so downstream classification (Engine 2) can still apply the
fallback code blocks.

All Azure OpenAI work runs inside the ``duke-api`` container; the host
proxy (Zscaler) blocks egress to the AOAI endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

from ms_duke_je_common.executor.task_context import spawn_in_request_context
from ms_duke_je_common.extraction.worksheet_scan import coerce_decimal
from ms_duke_je_common.vision.pdf_to_images import pdf_to_images

from src.models.gocoll_models import (
    GoCollBatch,
    GoCollCheck,
    GoCollCodeBlock,
    GoCollExtraction,
    GoCollTransaction,
)
from src.source_parsers.gocoll_pdf_parser import (
    batch_number_from_filename,
    batch_number_from_text,
    get_pdf_pages,
)
from src.source_parsers.image_tiling import build_page_images
from src.source_parsers.vision_extractor import extract_from_pdf

logger = logging.getLogger(__name__)

_GO_FORM_TYPE = "gocoll_go_form"


def _code_dim(value) -> str:
    """Normalize a code-block dimension, treating model placeholder zero as blank."""
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    return "" if text == "0" else text


def _code_block_from_line(line: dict) -> GoCollCodeBlock:
    """Build a :class:`GoCollCodeBlock` from a validated go-form line dict."""
    return GoCollCodeBlock(
        business_unit=_code_dim(line.get("business_unit", "")),
        account=_code_dim(line.get("account", "")),
        resource_type=_code_dim(line.get("resource_type", "")),
        operating_unit=_code_dim(line.get("operating_unit", "")),
        resp_center=_code_dim(line.get("resp_center", "")),
        project=_code_dim(line.get("project", "")),
        activity_id=_code_dim(line.get("activity_id", "")),
        process=_code_dim(line.get("process", "")),
        location=_code_dim(line.get("location", "")),
        product=_code_dim(line.get("product", "")),
        affiliate=_code_dim(line.get("affiliate", "")),
        alloc_pool=_code_dim(line.get("alloc_pool", "")),
    )


async def _vision_go_form(
    image_paths: list[Path],
    aoai_client,
    model: str,
    fallback_model: str | None,
) -> tuple[list[dict], float, str | None, int, int]:
    """Run the GO-form vision pass over one or more page images.

    Returns ``(lines, confidence, error, prompt_tokens, completion_tokens)``
    where ``lines`` is the list of validated distribution dicts (empty on
    failure) and the token counts feed the governance cost tracker.
    """
    if not image_paths:
        return [], 0.0, "no GO-form image to extract", 0, 0

    result = await extract_from_pdf(
        image_paths=image_paths,
        pdf_type=_GO_FORM_TYPE,
        aoai_client=aoai_client,
        model=model,
        fallback_model=fallback_model,
    )
    pt = int(result.prompt_tokens or 0)
    ct = int(result.completion_tokens or 0)
    if not result.success:
        return [], 0.0, result.error or "vision extraction failed", pt, ct

    lines = result.data.get("lines", []) if isinstance(result.data, dict) else []
    conf = 0.0
    if isinstance(result.confidence, dict):
        conf = float(result.confidence.get("lines", 0.0) or 0.0)
    return list(lines), conf, None, pt, ct


def _digits(value) -> str:
    """Return only numeric identifier characters from a model-read field."""
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _dump_vision_output(batch: GoCollBatch, page_results: list[dict]) -> None:
    """Persist the raw per-page vision reads + grouped checks to a JSON file.

    Gated by the ``GOCOLL_VISION_DUMP_DIR`` env var (no dump when unset). Written
    once per batch as ``vision_<batch>.json`` for offline inspection of what the
    model returned and how pages were grouped into checks.
    """
    dump_dir = os.getenv("GOCOLL_VISION_DUMP_DIR")
    if not dump_dir:
        return
    try:
        out_dir = Path(dump_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "batch_number": batch.batch_number,
            "source_pdf": batch.source_pdf,
            "deposit_control": {
                "wf_deposit_gross": batch.wf_deposit_gross,
                "wf_deposit_booked": batch.wf_deposit_booked,
                "wf_return_items": batch.wf_return_items,
            },
            "pages": [
                {
                    "page_index": r.get("page_index"),
                    "check_number": r.get("check_number"),
                    "batch_number": r.get("batch_number"),
                    "sequence_number": r.get("sequence_number"),
                    "check_amount": r.get("check_amount"),
                    "form_total": r.get("form_total"),
                    "confidence": r.get("conf"),
                    "error": r.get("error"),
                    "lines": r.get("lines"),
                }
                for r in sorted(page_results, key=lambda x: x.get("page_index", 0))
            ],
            "checks": [
                {
                    "check_number": c.check_number,
                    "batch_number": c.batch_number,
                    "sequence_number": c.sequence_number,
                    "check_amount": c.check_amount,
                    "source_pages": c.source_pages,
                    "lines": [
                        {
                            "sequence": ln.sequence,
                            "amount": ln.amount,
                            "line_desc": ln.line_desc,
                            "code_block": vars(ln.code_block)
                            if ln.code_block is not None else None,
                        }
                        for ln in c.lines
                    ],
                }
                for c in batch.checks
            ],
        }
        safe_batch = batch.batch_number or "unknown"
        out_path = out_dir / f"vision_{safe_batch}.json"
        out_path.write_text(
            json.dumps(payload, default=str, indent=2), encoding="utf-8"
        )
        logger.info("GOCOLL vision output dumped to %s", out_path)
    except Exception:  # noqa: BLE001 -- dump is best-effort diagnostics
        logger.exception("GOCOLL vision output dump failed")


async def _vision_page(
    image_path: Path,
    aoai_client,
    model: str,
    fallback_model: str | None,
) -> dict:
    """Read ONE batch-PDF page with the unified GOCOLL prompt.

    Returns a dict with the coding ``lines`` (empty on a non-coding page) plus
    the check-identifying fields printed on the page (``check_number`` /
    ``batch_number`` / ``sequence_number`` / ``check_amount``) and the coding
    grid's printed ``form_total``. ``error`` is set (and the rest defaulted)
    when the vision call fails.
    """
    # Send the page as the whole image plus higher-resolution overlapping tiles
    # so small code-block digits are read at full render DPI (improvement A).
    # Image tiling is CPU/disk work. Run it outside the event loop so the
    # executor heartbeat can continue while pages are being prepared.
    page_images, tile_dir = await asyncio.to_thread(
        build_page_images,
        image_path,
    )

    try:
        result = await extract_from_pdf(
            image_paths=page_images,
            pdf_type=_GO_FORM_TYPE,
            aoai_client=aoai_client,
            model=model,
            fallback_model=fallback_model,
        )
    finally:
        if tile_dir:
            shutil.rmtree(tile_dir, ignore_errors=True)

    if not result.success:
        return {
            "lines": [], "check_number": "", "batch_number": "",
            "sequence_number": "", "check_amount": Decimal("0"),
            "transaction_total": Decimal("0"),
            "form_total": Decimal("0"), "conf": 0.0,
            "prompt_tokens": int(result.prompt_tokens or 0),
            "completion_tokens": int(result.completion_tokens or 0),
            "error": result.error or "vision extraction failed",
        }

    data = result.data if isinstance(result.data, dict) else {}
    conf = 0.0
    if isinstance(result.confidence, dict):
        conf = float(result.confidence.get("lines", 0.0) or 0.0)

    return {
        "lines": list(data.get("lines", []) or []),
        "check_number": _digits(data.get("check_number", "")),
        "batch_number": str(data.get("batch_number", "") or "").strip(),
        "sequence_number": str(data.get("sequence_number", "") or "").strip(),
        "check_amount": abs(coerce_decimal(data.get("check_amount"))),
        "transaction_total": abs(coerce_decimal(data.get("transaction_total"))),
        "form_total": abs(coerce_decimal(data.get("form_total"))),
        "conf": conf,
        "prompt_tokens": int(result.prompt_tokens or 0),
        "completion_tokens": int(result.completion_tokens or 0),
        "error": None,
    }


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".gif", ".bmp"}


def _go_form_images(path: str | Path, image_dpi: int) -> list[Path]:
    """Return page images for a GO-form file (image passthrough or PDF render)."""
    path = Path(path)
    if path.suffix.lower() in _IMAGE_EXTS:
        return [path]
    return pdf_to_images(path, dpi=image_dpi)


async def _attach_go_form_files(
    extraction: GoCollExtraction,
    go_form_paths: list[str | Path],
    aoai_client,
    model: str,
    fallback_model: str | None,
    image_dpi: int,
) -> None:
    """Apply separately-supplied GO Collection Forms to their matching batches.

    Real GOCOLL GO forms arrive as separate attachments (email/file), *not* in
    the lockbox batch PDF. Each form carries the coded GL distribution split for
    a batch's deposit. When present, the form's coded lines REPLACE that batch's
    text-tier (deposit-amount) fallback lines. If the coded revenue total differs
    from the deposited check total, the gap is flagged for analyst review (no plug
    is fabricated); the batch still nets to 0 as the cash line is -SUM(coded).
    """
    if not go_form_paths:
        return
    if aoai_client is None:
        extraction.warnings.append(
            "GO-form file(s) supplied but no Azure OpenAI client - not applied"
        )
        return

    by_batch: dict[str, list[Path]] = {}
    for gp in go_form_paths:
        gp = Path(gp)
        bn = batch_number_from_filename(gp) or ""
        by_batch.setdefault(bn, []).append(gp)

    batch_index = {b.batch_number: b for b in extraction.batches}

    # Shared bounded concurrency for all GO-form page reads across batches.
    _gf_conc = max(1, int(os.getenv("GOCOLL_VISION_CONCURRENCY", "8")))
    _gf_sem = asyncio.Semaphore(_gf_conc)

    for bn, paths in by_batch.items():
        batch = batch_index.get(bn)
        if batch is None:
            # Single-batch period with an unlabelled GO form -> apply to it.
            if bn == "" and len(extraction.batches) == 1:
                batch = extraction.batches[0]
            else:
                extraction.warnings.append(
                    f"GO-form(s) for batch {bn or '?'} but no matching batch PDF"
                )
                continue

        deposit_total = batch.check_total  # sum of deposited checks (pre-replace)
        coded: list[GoCollTransaction] = []
        last_conf = 1.0

        # Render every GO-form page up front, then read them concurrently. The
        # reads are independent, so a multi-page form no longer extracts one
        # page at a time; results are folded back in render order below.
        work: list[tuple[Path, Path]] = []  # (source_path, image_path)
        for gp in paths:
            # page at a time; results are folded back in render order below.
            try:
                images = _go_form_images(gp, image_dpi)
            except Exception as exc:  # noqa: BLE001 - surfaced as a batch error
                batch.errors.append(f"GO-form render failed ({gp.name}): {exc}")
                continue
            work.extend((Path(gp), img) for img in images)

        async def _read(img: Path):
            async with _gf_sem:
                return await _vision_go_form(
                    [img], aoai_client, model, fallback_model
                )

        read_results = await asyncio.gather(*(_read(img) for _gp, img in work))

        for (gp, _img), (lines, conf, error, pt, ct) in zip(work, read_results, strict=True):
            batch.prompt_tokens += int(pt or 0)
            batch.completion_tokens += int(ct or 0)
            batch.llm_calls += 1
            if error:
                batch.errors.append(f"GO-form vision failed ({gp.name}): {error}")
                continue
            last_conf = conf or last_conf
            for line in lines:
                coded.append(
                    GoCollTransaction(
                        batch_number=batch.batch_number,
                        sequence=0,  # renumbered below
                        code_block=_code_block_from_line(line),
                        amount=coerce_decimal(line.get("amount", Decimal("0"))),
                        line_desc=str(line.get("line_desc", "") or ""),
                        extraction_tier="go_form",
                        confidence=conf,
                    )
                )

        if not coded:
            extraction.warnings.append(
                f"GO-form(s) for batch {batch.batch_number} yielded no coded lines - "
                "keeping text-tier fallback"
            )
            continue

        coded_total = sum((abs(t.amount) for t in coded), Decimal("0"))
        deposit_abs = abs(deposit_total)
        if deposit_abs and abs(coded_total - deposit_abs) > Decimal("0.005"):
            diff = coded_total - deposit_abs
            reason = (
                f"GO-form coded total ({coded_total}) does not reconcile to the "
                f"deposited check total ({deposit_abs}) (off {diff}); flagged for review"
            )
            coded[0].needs_review = True
            coded[0].review_reason = reason
            coded[0].warnings.append(reason)
            logger.info(
                "GOCOLL batch %s: coded=%s deposit=%s GAP=%s flagged",
                batch.batch_number, coded_total, deposit_abs, diff,
            )

        batch.checks = [
            GoCollCheck(
                batch_number=batch.batch_number,
                check_amount=abs(deposit_total),
                lines=coded,
            )
        ]
        for i, txn in enumerate(batch.transactions, start=1):
            txn.sequence = i
        logger.info(
            "GOCOLL batch %s: applied %d GO-form coded line(s) from %d file(s) "
            "(deposit=%s, coded=%s)",
            batch.batch_number, len(coded), len(paths), deposit_abs, coded_total,
        )


def _page_progress_message(
    source_pdf: str,
    completed_pages: int,
    total_pages: int,
    *,
    range_size: int = 10,
) -> str | None:
    """Return filename-scoped progress once each page range is complete."""
    if total_pages <= 0 or completed_pages <= 0:
        return None
    completed_pages = min(completed_pages, total_pages)
    if completed_pages % range_size != 0 and completed_pages != total_pages:
        return None
    range_start = ((completed_pages - 1) // range_size) * range_size + 1
    filename = Path(source_pdf).name or "uploaded PDF"
    # File Manager materialization may prepend a content hash and add a
    # duplicate-download suffix, neither of which is meaningful to the user.
    filename = re.sub(r"^[0-9a-fA-F]{16,}_", "", filename)
    filename = re.sub(r"\(\d+\)(?=\.[^.]+$)", "", filename)
    return (
        f"Extracting {filename}: pages {range_start}-{completed_pages} "
        f"of {total_pages}..."
    )


async def _extract_vision_only_batch(
    batch: GoCollBatch,
    image_paths: list[Path] | None,
    aoai_client,
    model: str,
    fallback_model: str | None,
    *,
    page_stream: AsyncIterator[tuple[int, Path]] | None = None,
    total_pages: int | None = None,
) -> None:
    """Extract an image-only batch via a single vision pass per page.

    Each page is read once with the unified prompt, then pages are grouped into
    checks: a page's check is keyed by its printed check number (fallback:
    batch/sequence). A page that carries coding lines but none of those keys is
    appended to the current (most-recent) check, since a check's pages are laid
    out contiguously in the batch PDF (summary -> check image -> GO form).

    Extraction is stored on ``batch.checks``; reconciliation (batch-level) and
    assembly (per-line) consume the derived ``batch.transactions`` view.
    """
    max_conc = max(1, int(os.getenv("GOCOLL_VISION_CONCURRENCY", "8")))
    _page_sem = asyncio.Semaphore(max_conc)
    total_pages = total_pages if page_stream is not None else len(image_paths or [])
    _pages_done = 0
    logger.info(
        "GOCOLL batch %s: reading %d page image(s) with vision concurrency=%d",
        batch.batch_number or batch.source_pdf,
        _total_pages,
        max_conc,
    )

    async def _read_page(page_index: int, image_path: Path) -> dict:
        nonlocal _pages_done
        async with _page_sem:
            res = await _vision_page(image_path, aoai_client, model, fallback_model)
            res["page_index"] = page_index
            _pages_done += 1
            progress_message = _page_progress_message(
                batch.source_pdf,
                _pages_done,
                _total_pages,
            )
            logger.info(
                "GOCOLL batch %s page %03d: vision %s, lines=%d, check=%s, seq=%s, amount=%s",
                batch.batch_number or batch.source_pdf,
                page_index + 1,
                "ERROR" if res.get("error") else "OK",
                len(res.get("lines") or []),
                res.get("check_number") or "-",
                res.get("sequence_number") or "-",
                res.get("check_amount") or "-",
            )
            # Keep reasoning concise: one filename-scoped update per ten
            # completed pages, plus the final partial range.
            if progress_message:
                try:
                    from src.logic.progress_context import emit_stage_progress
                    await emit_stage_progress(
                        "extraction",
                        "active",
                        progress_message,
                    )
                except Exception:  # noqa: BLE001
                    pass
            return res

    if page_stream is None:
        page_tasks = [
            spawn_in_request_context(_read_page(index, path))
            for index, path in enumerate(image_paths or [])
        ]
    else:
        page_tasks = []
        try:
            async for index, path in page_stream:
                page_tasks.append(spawn_in_request_context(_read_page(index, path)))
        except BaseException:
            for task in page_tasks:
                task.cancel()
            await asyncio.gather(*page_tasks, return_exceptions=True)
            raise

    page_results = await asyncio.gather(*page_tasks)
    ok_pages = sum(1 for r in page_results if not r.get("error"))
    line_count = sum(len(r.get("lines") or []) for r in page_results if not r.get("error"))
    # Accumulate vision LLM usage for governance cost tracking (one call/page).
    batch.prompt_tokens += sum(int(r.get("prompt_tokens", 0) or 0) for r in page_results)
    batch.completion_tokens += sum(int(r.get("completion_tokens", 0) or 0) for r in page_results)
    batch.llm_calls += len(page_results)
    logger.info(
        "GOCOLL batch %s: completed page vision, ok_pages=%d/%d, extracted_lines=%d",
        batch.batch_number or batch.source_pdf,
        ok_pages,
        len(page_results),
        line_count,
    )

    # Content-based batch number: take the most frequently read batch number off
    # the pages and override the filename placeholder.
    from collections import Counter

    _batch_counts: Counter[str] = Counter(
        r["batch_number"]
        for r in page_results
        if not r.get("error") and r.get("batch_number")
    )

    if _batch_counts:
        _content_batch = _batch_counts.most_common(1)[0][0]
        if _content_batch and not batch_number_from_filename(batch.source_pdf):
            # Only adopt the vision-read batch number when the filename gave nothing.
            logger.info(
                "GOCOLL batch %s: batch number %s read from page content "
                "(filename fallback)",
                batch.source_pdf, _content_batch,
            )
            batch.batch_number = _content_batch
        elif _content_batch and _content_batch != batch.batch_number:
            logger.info(
                "GOCOLL batch %s: filename batch number %s kept (content read %s)",
                batch.source_pdf, batch.batch_number, _content_batch,
            )

    # Group pages into checks in page order, tracking the current check so a
    # keyless coding page attaches to the check whose pages it follows.
    key_index: dict[tuple, GoCollCheck] = {}
    current: GoCollCheck | None = None
    for res in sorted(page_results, key=lambda r: r["page_index"]):
        page_index = res["page_index"]
        if res.get("error"):
            batch.errors.append(f"page {page_index}: {res['error']}")
            continue

        check_no = _digits(res["check_number"])
        seq_no = _digits(res["sequence_number"])
        if check_no:
            key = ("chk", check_no)
        elif seq_no:
            key = ("seq", res["batch_number"] or batch.batch_number, seq_no)
        else:
            key = None

        if key is not None:
            chk = key_index.get(key)
            if chk is None:
                chk = GoCollCheck(
                    check_number=check_no,
                    batch_number=res["batch_number"] or batch.batch_number,
                    sequence_number=res["sequence_number"],
                )
                batch.checks.append(chk)
                key_index[key] = chk
            current = chk
        else:
            # Orphan page (coding lines but no key). Attach to the current check;
            # a leading orphan opens a new anonymous check.
            if current is None:
                current = GoCollCheck(batch_number=batch.batch_number)
                batch.checks.append(current)
            chk = current

        chk.source_pages.append(page_index)
        # A check / WF page carries the deposited amount; keep the printed value.
        if res["check_amount"] and res["check_amount"] > 0:
            chk.check_amount = res["check_amount"]
            if not chk.check_number and check_no:
                chk.check_number = check_no
        if res["transaction_total"] and res["transaction_total"] > 0:
            chk.transaction_total = res["transaction_total"]

        for line in res["lines"]:
            chk.lines.append(
                GoCollTransaction(
                    batch_number=chk.batch_number or batch.batch_number,
                    sequence=0,  # renumbered in extract_batch
                    check_number=chk.check_number,
                    check_amount=chk.check_amount,
                    code_block=_code_block_from_line(line),
                    amount=coerce_decimal(line.get("amount", Decimal("0"))),
                    line_desc=str(line.get("line_desc", "") or ""),
                    source_page=page_index,
                    extraction_tier="vision",
                    confidence=res["conf"],
                )
            )

    # Sync each line's check-level fields to its final parent check values
    # (check_amount may have been read on a later page than the coding page).
    for chk in batch.checks:
        for ln in chk.lines:
            ln.check_number = chk.check_number
            ln.check_amount = chk.check_amount
            ln.batch_number = chk.batch_number or batch.batch_number

    # Batch-level deposit control (a fallback to the WF Excel report in Engine 4):
    # the sum of the per-check deposited amounts. Return items are sourced from
    # the Treasury BAI 566 feed, not from vision.
    gross = sum((c.check_amount for c in batch.checks), Decimal("0"))
    batch.wf_deposit_gross = gross or None
    batch.wf_deposit_booked = gross or None
    batch.wf_return_items = Decimal("0")

    logger.info(
        "GOCOLL batch %s: %d check(s), %d coded line(s), deposit control=%s",
        batch.batch_number,
        len(batch.checks),
        sum(len(c.lines) for c in batch.checks),
        gross,
    )

    _dump_vision_output(batch, page_results)

    if not batch.transactions:
        batch.errors.append(
            "image-only batch yielded no GO-form distributions from vision"
        )


def _pdf_page_count(pdf_path: Path) -> int:
    import fitz
    document = fitz.open(str(pdf_path))
    try:
        return len(document)
    finally:
        document.close()


async def _stream_pdf_pages(
    pdf_path: Path,
    *,
    dpi: int,
) -> AsyncIterator[tuple[int, Path]]:
    """Render pages serially in a worker while yielding each completed PNG."""
    import fitz

    output_dir = pdf_path.parent / f".{pdf_path.stem}_pages"
    output_dir.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[int, Path] | Exception | None] = asyncio.Queue()
    stop_requested = threading.Event()

    def _render() -> None:
        document = None
        try:
            document = fitz.open(str(pdf_path))
            matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
            for page_index, page in enumerate(document):
                if stop_requested.is_set():
                    break
                image_path = output_dir / f"page_{page_index + 1:03d}.png"
                page.get_pixmap(matrix=matrix).save(str(image_path))
                if stop_requested.is_set():
                    break
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    (page_index, image_path),
                )
            logger.info(
                "Converted %d pages from %s to PNG (PyMuPDF streaming)",
                len(document),
                pdf_path.name,
            )
        except Exception as exc:  # propagated to the async consumer
            if not stop_requested.is_set():
                loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            if document is not None:
                document.close()
            if not stop_requested.is_set():
                loop.call_soon_threadsafe(queue.put_nowait, None)

    render_task = asyncio.create_task(asyncio.to_thread(_render))
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item
        await render_task
    finally:
        stop_requested.set()
        if not render_task.done():
            render_task.cancel()
        await asyncio.gather(render_task, return_exceptions=True)


async def extract_batch(
    pdf_path: str | Path,
    aoai_client=None,
    model: str = "gpt-5.2",
    fallback_model: str | None = "gpt-5.2",
    image_dpi: int = 300,
) -> GoCollBatch:
    """Extract a single GOCOLL batch PDF into a :class:`GoCollBatch`."""
    pdf_path = Path(pdf_path)
    # -- Batch number: FILENAME-FIRST -----------------------------------------
    # The filename is the authoritative source (e.g. "Batch - 632.pdf" -> "632").
    # PDF content (the "Batch Number : N" label) is the fallback for files with
    # generic names that carry no batch identifier.
    filename_batch = batch_number_from_filename(pdf_path) or ""
    # Named batch PDFs do not need text-layer parsing; GOCOLL uses the full
    # rendered-page vision path for extraction, and the filename already
    # supplies the authoritative batch number. This avoids reading/parsing each
    # PDF twice before rendering its images. Keep the content fallback for
    # generic filenames.
    content_batch = ""
    if not filename_batch:
        # PDF parsing is a CPU/blocking-library operation. Keep it off the
        # request event loop so working heartbeats continue reaching SSE.
        pages = await asyncio.to_thread(get_pdf_pages, pdf_path)
        content_batch = batch_number_from_text(pages)
    batch_no = filename_batch or content_batch or ""
    batch = GoCollBatch(batch_number=batch_no, source_pdf=pdf_path.name)
    if filename_batch:
        logger.info(
            "GOCOLL batch %s: batch number %s read from filename",
            pdf_path.name, filename_batch,
        )
    elif content_batch:
        logger.info(
            "GOCOLL batch %s: batch number %s read from PDF content (filename fallback)",
            pdf_path.name, content_batch,
        )

    # GOCOLL renders above the shared 200-DPI default so the per-page tiles
    # built in _vision_page keep small code-block digits legible. Pages are
    # yielded to vision as soon as each PNG is ready, overlapping CPU rendering
    # with network/model latency without changing the configured DPI.
    render_dpi = max(image_dpi, int(os.getenv("GOCOLL_VISION_DPI", "400")))
    # Every batch is treated as image-only: all extraction is performed by the
    # vision/LLM tier on each rendered page. Deterministic PDF text parsing is
    # *not* used for distributions - the form/check pages are scanned images even
    # when the Wells Fargo summary pages happen to carry a text layer.
    logger.info(
        "GOCOLL batch %s: running full vision pass (LLM-only extraction)",
        pdf_path.name,
    )
    if aoai_client is None:
        batch.errors.append(
            "no Azure OpenAI client - vision-only extraction cannot run"
        )
    else:
        try:
            total_pages = await asyncio.to_thread(_pdf_page_count, pdf_path)
            if total_pages <= 0:
                batch.errors.append(
                    "no page images rendered - vision-only extraction cannot run"
                )
            else:
                await _extract_vision_only_batch(
                    batch,
                    None,
                    aoai_client,
                    model,
                    fallback_model,
                    page_stream=_stream_pdf_pages(pdf_path, dpi=render_dpi),
                    total_pages=total_pages,
                )
        except ImportError:
            try:
                image_paths = await asyncio.to_thread(
                    pdf_to_images,
                    pdf_path,
                    dpi=render_dpi,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced as a batch error
                batch.errors.append(f"pdf-to-image conversion failed: {exc}")
                image_paths = []
            if image_paths:
                await _extract_vision_only_batch(
                    batch, image_paths, aoai_client, model, fallback_model
                )
            else:
                batch.errors.append(
                    "no page images rendered - vision-only extraction cannot run"
                )
        except Exception as exc:  # noqa: BLE001 - surfaced as a batch error
            batch.errors.append(f"pdf-to-image conversion failed: {exc}")

    # Renumber to clean, contiguous 1..N sequences after any sub-line fan-out.
    for i, txn in enumerate(batch.transactions, start=1):
        txn.sequence = i
    return batch


async def extract_period(
    pdf_paths: list[str | Path],
    period_label: str = "",
    journal_date: str = "",
    aoai_client=None,
    model: str = "gpt-5.2",
    fallback_model: str | None = "gpt-5.2",
    image_dpi: int = 300,
    go_form_paths: list[str | Path] | None = None,
) -> GoCollExtraction:
    """Extract all batch PDFs for a GOCOLL period (Engine 1 entry point).

    Args:
        pdf_paths: The batch PDFs for the period (e.g. Batch-632, Batch-633).
        period_label: eFIS header label, e.g. "JAN2025_BATCH-632-633".
        journal_date: eFIS journal date, e.g. "2026-01-31".
        aoai_client: Azure OpenAI async client (None disables the vision tier).
        model: Primary vision deployment.
        fallback_model: Optional fallback vision deployment.
        image_dpi: Render DPI for page images.
        go_form_paths: Optional separately-supplied GO Collection Form files
            (PDF or image). When present they override the matching batch's
            text-tier coding with the form's coded GL distribution split.

    Returns:
        A :class:`GoCollExtraction` aggregating every batch.
    """
    extraction = GoCollExtraction(
        period_label=period_label,
        journal_date=journal_date,
    )

    # Fan out batch extraction across PDFs with bounded concurrency. Batches
    # are fully independent (each renders + reads its own pages), so this is a
    # large speed-up for multi-batch periods. Per-batch faults are isolated:
    # one bad PDF records an error and never aborts the others. Results are
    # re-ordered to match the input PDF order so output is deterministic.
    batch_conc = max(1, int(os.getenv("GOCOLL_BATCH_CONCURRENCY", "4")))
    _batch_sem = asyncio.Semaphore(batch_conc)

    async def _extract_one(idx: int, pdf_path: str | Path):
        async with _batch_sem:
            try:
                batch = await extract_batch(
                    pdf_path,
                    aoai_client=aoai_client,
                    model=model,
                    fallback_model=fallback_model,
                    image_dpi=image_dpi,
                )
                return idx, batch, None
            except Exception as exc:  # noqa: BLE001 - one bad batch must not kill the run
                logger.exception("GOCOLL extraction failed for %s", pdf_path)
                return idx, None, f"{Path(pdf_path).name}: {exc}"

    results = await asyncio.gather(
        *(_extract_one(i, p) for i, p in enumerate(pdf_paths))
    )
    for _idx, batch, err in sorted(results, key=lambda r: r[0]):
        if err is not None:
            extraction.errors.append(err)
            continue
        if batch is not None:
            extraction.batches.append(batch)

    # Apply separately-supplied GO Collection Forms (coded GL split) before
    # surfacing per-batch errors, since a form may clear a batch's gaps.
    if go_form_paths:
        await _attach_go_form_files(
            extraction, go_form_paths, aoai_client, model, fallback_model, image_dpi
        )

    for batch in extraction.batches:
        extraction.errors.extend(f"{batch.source_pdf}: {e}" for e in batch.errors)

    # Aggregate vision LLM usage across all batches for governance cost tracking.
    extraction.prompt_tokens = sum(b.prompt_tokens for b in extraction.batches)
    extraction.completion_tokens = sum(b.completion_tokens for b in extraction.batches)
    extraction.llm_calls = sum(b.llm_calls for b in extraction.batches)
    extraction.vision_model = model

    logger.info(
        "GOCOLL period extraction complete: %d batch(es), %d transaction(s), "
        "%d error(s), %d vision call(s), %d tokens",
        len(extraction.batches),
        extraction.transaction_count,
        len(extraction.errors),
        extraction.llm_calls,
        extraction.prompt_tokens + extraction.completion_tokens,
    )
    return extraction