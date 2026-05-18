"""Generate / serve theme preview images for the dashboard.

Resolution order (first hit wins):
  1. <theme_dir>/preview.png|jpg|jpeg   (hand-supplied; never regenerated)
  2. <theme_dir>/.cache/preview.png     (auto-generated on first miss; reused)
  3. on miss, generate based on background_type:
       video → ffmpeg -ss 0 ... first frame to .cache/preview.png
       gif   → PIL extract frame 0 to .cache/preview.png
       image → serve the source image directly (no resize for now)

Falls back gracefully when ffmpeg / PIL plugins aren't present — caller
just sees a None and the UI shows a glyph instead of a thumbnail.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def _hand_supplied_preview(theme_dir: Path) -> Optional[Path]:
    for cand in ("preview.png", "preview.jpg", "preview.jpeg"):
        p = theme_dir / cand
        if p.exists():
            return p
    return None


def _cache_path(theme_dir: Path) -> Path:
    return theme_dir / ".cache" / "preview.png"


def _video_first_frame(src: Path, dst: Path) -> bool:
    """Extract the first frame of a video file to `dst` via ffmpeg.
    Returns True on success."""
    try:
        from .video_utils import get_ffmpeg_path
    except ImportError:
        return False
    ffmpeg = get_ffmpeg_path()
    if ffmpeg is None:
        log.debug("ffmpeg not available; can't generate preview for %s", src.name)
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-y",
        "-ss", "0",
        "-i", str(src),
        "-vframes", "1",
        "-an",
        str(dst),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("ffmpeg preview for %s failed: %s", src.name, exc)
        return False
    if proc.returncode != 0:
        log.warning(
            "ffmpeg preview for %s exited %d (stderr tail: %s)",
            src.name, proc.returncode, proc.stderr[-300:]
        )
        return False
    return dst.exists()


def _gif_first_frame(src: Path, dst: Path) -> bool:
    """Pull frame 0 of a GIF into a PNG via PIL."""
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(src) as im:
            im.seek(0)
            dst.parent.mkdir(parents=True, exist_ok=True)
            im.convert("RGBA").save(dst, format="PNG")
        return dst.exists()
    except Exception as exc:
        log.warning("PIL gif preview for %s failed: %s", src.name, exc)
        return False


def resolve_preview(
    theme_dir: Path,
    background_type: str,
    background_path: Optional[Path],
) -> Optional[Path]:
    """Return a usable preview image path for the theme, generating one
    if necessary. Returns None when there's nothing to show.
    """
    theme_dir = Path(theme_dir)

    # 1) hand-supplied
    hand = _hand_supplied_preview(theme_dir)
    if hand is not None:
        return hand

    # 2) cached auto-generated
    cache = _cache_path(theme_dir)
    if cache.exists():
        return cache

    # 3) generate based on type
    if not background_path or not background_path.exists():
        return None
    if background_type == "image":
        # No generation needed — just serve the source image. Cheaper
        # than re-encoding and the dashboard displays it at 4:1 anyway.
        return background_path
    if background_type == "video":
        if _video_first_frame(background_path, cache):
            return cache
    if background_type == "gif":
        if _gif_first_frame(background_path, cache):
            return cache
    return None
