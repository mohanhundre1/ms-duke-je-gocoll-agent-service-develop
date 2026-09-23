"""Higher-resolution page tiling for GOCOLL vision extraction (improvement A).

Azure OpenAI vision caps each image's long edge at ~2048px for ``detail:"high"``,
so a full Letter page rendered even at 300+ DPI is downscaled to ~186 effective
DPI and the small GO-form code-block digits (5-digit BU, 7-char account) become
hard to read. Splitting the page into overlapping tiles keeps each tile under the
cap, so digits are transcribed at (or near) full render DPI. The whole page is
still sent first so the model keeps global layout/grouping context.

Behaviour is env-driven and GOCOLL-scoped:
    GOCOLL_VISION_TILING        on/off       (default on)
    GOCOLL_VISION_TILE_COLS     int          (default 2)
    GOCOLL_VISION_TILE_ROWS     int          (default 2)
    GOCOLL_VISION_TILE_OVERLAP  float        (default 0.10, fraction of a tile edge)
    GOCOLL_VISION_INCLUDE_FULL_PAGE 1|0      (default 1 - send whole page first)
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

try:
    from PIL import Image
except Exception:  # noqa: BLE001 - Pillow missing: tiling silently disabled
    Image = None


_FALSE = {"0", "off", "false", "no"}


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() not in _FALSE


def _int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def tiling_enabled() -> bool:
    return Image is not None and _flag("GOCOLL_VISION_TILING", "on")


def build_page_images(image_path: Path) -> tuple[list[Path], str | None]:
    """Return the image list to send for ONE page, plus a temp dir to clean up.

    When tiling is disabled/unavailable the original page image is returned
    unchanged with a ``None`` temp dir. Otherwise returns (optional full page)
    followed by overlapping tiles written under a fresh temp dir that the caller
    MUST remove after the vision call completes.
    """
    image_path = Path(image_path)
    if not tiling_enabled():
        return [image_path], None

    cols = _int("GOCOLL_VISION_TILE_COLS", 2)
    rows = _int("GOCOLL_VISION_TILE_ROWS", 2)
    if cols == 1 and rows == 1:
        return [image_path], None
    overlap = _float("GOCOLL_VISION_TILE_OVERLAP", 0.10)
    include_full = _flag("GOCOLL_VISION_INCLUDE_FULL_PAGE", "1")

    try:
        img = Image.open(image_path)
        img.load()
    except Exception:  # noqa: BLE001 - unreadable image: fall back to original
        return [image_path], None

    width, height = img.size
    base_w = width / cols
    base_h = height / rows
    ov_w = base_w * overlap
    ov_h = base_h * overlap

    tmp = tempfile.mkdtemp(prefix="gocoll_tiles_")
    images: list[Path] = [image_path] if include_full else []
    stem = image_path.stem
    for r in range(rows):
        for c in range(cols):
            left = max(0, int(round(c * base_w - ov_w)))
            upper = max(0, int(round(r * base_h - ov_h)))
            right = min(width, int(round((c + 1) * base_w + ov_w)))
            lower = min(height, int(round((r + 1) * base_h + ov_h)))
            if right <= left or lower <= upper:
                continue
            crop = img.crop((left, upper, right, lower))
            out = Path(tmp) / f"{stem}_r{r}c{c}.png"
            crop.save(out, format="PNG")
            images.append(out)

    if len(images) <= (1 if include_full else 0):
        shutil.rmtree(tmp, ignore_errors=True)
        return [image_path], None
    return images, tmp