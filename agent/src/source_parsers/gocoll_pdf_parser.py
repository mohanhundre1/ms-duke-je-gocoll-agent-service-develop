"""GOColl batch-PDF parser - Engine 1, text-layer tier.

A GOColl batch PDF is a repeating sequence per transaction:

    Transaction Summary (Lockbox 604205 "GO COLLECTION", Site CLT,
        deposit account, Check#, Check Amount, Batch#)
    -> Check Front Image
    -> GO-Collection-Form image (the accounting code block)

This module reads the **text layer** with PyMuPDF and:
1. detects each Transaction Summary block and its fields, and
2. segments the PDF into per-transaction page ranges so the vision
   tier (`go_form_vision`) can target the GO-Collection-Form page.

Batches whose pages have no text layer (e.g. fully scanned batch 632)
yield no summaries here; the coordinator falls back to a vision pass.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from src.models.gocoll_models import GoCollTransaction

logger = logging.getLogger(__name__)

# -- Regex patterns for the Transaction Summary block -----------------------

_LOCKBOX_PAT = re.compile(r"Lockbox\s*[:#]?\s*(\d{4,7})", re.IGNORECASE)
_SITE_PAT = re.compile(r"\bsite\s*[:#]?\s*([A-Z]{2,4})\b", re.IGNORECASE)
_DEPOSIT_ACCT_PAT = re.compile(
    r"(?:Deposit\s*Account|Account)\s*[:#]?\s*(\d{6,})", re.IGNORECASE
)
_CHECK_NO_PAT = re.compile(r"Check\s*#?\s*[:#]?\s*(\d{3,})", re.IGNORECASE)
_CHECK_AMT_PAT = re.compile(
    r"Check\s*Amount\s*[:#]?\s*\$?\s*([\d,]+\.\d{2})", re.IGNORECASE
)

# Match the real lockbox label "Batch Number : 633" (and "Batch #633",
# "Batch: 632"). The optional "Number"/"No"/"#" MUST sit directly after the
# word "Batch", so this never picks up a stray digit run from an unrelated
# field such as "Check Account Number : 38976463" or "Sequence Number : 5".
_BATCH_PAT = re.compile(
    r"Batch\s*(?:Number|No\.?|#)?\s*[:#]?\s*(\d{2,})", re.IGNORECASE
)

_SUMMARY_MARKER = re.compile(
    r"(Transaction\s+Summary|GO\s*COLLECTION)", re.IGNORECASE
)
_GO_FORM_MARKER = re.compile(
    r"(GO\s*COLLECTION\s*FORM|Collection\s*Form|Account\s*Distribution)",
    re.IGNORECASE,
)

# Scanned GO-Collection-Form / check-image pages carry no form text in the
# PDF text layer - only a thin caption like "Invoice 1 Front Image". These are
# the pages the vision tier must read for the accounting code block.
_IMAGE_PAGE_MARKER = re.compile(
    r"(Front\s*Image|Back\s*Image|Invoice\s*\d+\s*(?:Front|Back)|Check\s*Image)",
    re.IGNORECASE,
)

# Pages with fewer characters than this and no Transaction Summary are treated
# as scanned images (the GO-form pages live here).
_MIN_TEXT_PAGE = 60

def _parse_amount(s: str | None) -> Decimal:
    """Parse a money string (commas, $, parentheses) into Decimal."""
    if not s:
        return Decimal("0")
    raw = s.strip()
    neg = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()").replace("$", "").replace(",", "").strip()
    if not raw:
        return Decimal("0")
    try:
        val = Decimal(raw)
    except InvalidOperation:
        return Decimal("0")
    return -val if neg else val

@dataclass
class _PageText:
    index: int          # 0-based page index
    text: str

def get_pdf_pages(pdf_path: str | Path) -> list[_PageText]:
    """Extract the text layer for each page with PyMuPDF."""
    from ms_duke_je_common.extraction import extract_pdf_text_pages
    return [_PageText(index=i, text=t or "") for i, t in enumerate(extract_pdf_text_pages(pdf_path))]

def has_text_layer(pages: list[_PageText], min_chars: int = 50) -> bool:
    """True when the PDF carries enough selectable text to parse summaries."""
    from ms_duke_je_common.extraction import has_extractable_text
    return has_extractable_text([p.text for p in pages], min_chars=min_chars)

def _first(pattern: re.Pattern[str], text: str) -> str:
    m = pattern.search(text)
    return m.group(1).strip() if m else ""

def _parse_summary_block(text: str) -> dict[str, str] | None:
    """Parse one Transaction Summary block of text into raw fields."""
    if not _SUMMARY_MARKER.search(text):
        return None

    fields = {
        "lockbox": _first(_LOCKBOX_PAT, text),
        "site": _first(_SITE_PAT, text),
        "deposit_account": _first(_DEPOSIT_ACCT_PAT, text),
        "check_number": _first(_CHECK_NO_PAT, text),
        "check_amount": _first(_CHECK_AMT_PAT, text),
        "batch_number": _first(_BATCH_PAT, text),
    }
    # Require at least a check number or check amount to count as a txn.
    if not (fields["check_number"] or fields["check_amount"]):
        return None
    return fields

def parse_batch_pdf(
    pdf_path: str | Path,
    batch_hint: str | None = None,
) -> tuple[list[GoCollTransaction], dict[int, list[int]]]:
    """Parse a batch PDF text layer into transaction summary stubs.

    Args:
        pdf_path: Path to the batch PDF.
        batch_hint: Batch number to use when a summary omits it (e.g.
            derived from the filename "Batch - 632.pdf").

    Returns:
        A tuple of:
        - the list of ``GoCollTransaction`` stubs (summary populated,
          ``code_block`` left empty for the vision tier to fill), and
        - a mapping of ``transaction sequence (1-based) -> list of GO-form
          page indexes (0-based)`` to target the vision pass. Each
          transaction may own several scanned form/image pages. The
          mapping is empty when no GO-form page can be located.
    """
    pdf_path = Path(pdf_path)
    pages = get_pdf_pages(pdf_path)
    if not has_text_layer(pages):
        logger.info(
            "GOColl batch %s: no usable text layer - defer to vision tier",
            pdf_path.name,
        )
        return [], {}

    transactions: list[GoCollTransaction] = []
    go_form_pages: dict[int, list[int]] = {}
    seq = 0

    for page in pages:
        fields = _parse_summary_block(page.text)
        if fields is None:
            # Non-summary pages between transactions are the scanned
            # GO-Collection-Form / check images. They carry no form text in
            # the PDF layer - only a thin "Invoice N Front Image" caption -
            # so match the caption OR a near-empty page and route every such
            # page of the current transaction to the vision tier.
            stripped = page.text.strip()
            is_image_page = bool(
                _GO_FORM_MARKER.search(page.text)
                or _IMAGE_PAGE_MARKER.search(page.text)
                or len(stripped) < _MIN_TEXT_PAGE
            )
            if transactions and is_image_page:
                go_form_pages.setdefault(
                    transactions[-1].sequence, []
                ).append(page.index)
            continue

        seq += 1
        batch_number = (batch_hint or "") or fields["batch_number"]
        check_amount = _parse_amount(fields["check_amount"])
        txn = GoCollTransaction(
            batch_number=batch_number,
            sequence=seq,
            check_number=fields["check_number"],
            check_amount=check_amount,
            # The deposited check amount is the transaction's distribution
            # amount. When a GO-Collection-Form is present, the vision tier
            # overrides this with the form's coded split; absent a form
            # (e.g. batch 633 - summaries + invoice images only) the whole
            # check posts as one coded line so the batch still balances.
            amount=check_amount,
            lockbox=fields["lockbox"],
            site=fields["site"],
            deposit_account=fields["deposit_account"],
            source_page=page.index,
            extraction_tier="text",
        )
        transactions.append(txn)

    logger.info(
        "GOColl batch %s: parsed %d transaction summaries "
        "(%d txn(s) with GO-form pages, %d page(s) total)",
        pdf_path.name,
        len(transactions),
        len(go_form_pages),
        sum(len(v) for v in go_form_pages.values()),
    )
    return transactions, go_form_pages

def batch_number_from_filename(pdf_path: str | Path) -> str | None:
    """Derive a batch number from a filename like 'Batch - 632.pdf'.

    Primary source for the batch number. Content-based detection is the
    fallback when the filename carries no recognisable digit run.
    """
    stem = Path(pdf_path).stem
    batch_match = re.search(
        r"\bbatch\b\s*(?:number|no\.?|#)?\s*[-_:#]?\s*(\d{2,})\b",
        stem,
        re.IGNORECASE,
    )
    if batch_match:
        return batch_match.group(1)

    # File Manager materialization can prepend a content hash and Windows can
    # append a duplicate-download suffix. Neither belongs to the source name.
    stem = re.sub(r"^[0-9a-fA-F]{16,}_", "", stem)
    stem = re.sub(r"\s*\(\d+\)$", "", stem)
    m = re.search(r"(\d{2,})", stem)
    return m.group(1) if m else None

def batch_number_from_text(pages: list[_PageText]) -> str | None:
    """Read the authoritative batch number from the PDF text layer.

    Scans every page for the lockbox ``Batch Number : N`` label and returns the
    most frequently printed value (each transaction summary repeats it). Returns
    ``None`` when no batch label is present (e.g. a fully scanned, image-only
    batch with no text layer - those defer to the vision tier).
    """
    from collections import Counter

    counts: Counter[str] = Counter()
    for page in pages:
        for m in _BATCH_PAT.finditer(page.text or ""):
            counts[m.group(1).strip()] += 1
    if not counts:
        return None
    # Most-printed value wins; ties broken by the larger count then value.
    return counts.most_common(1)[0][0]