"""Pre-process theme video assets — rotation, transcoding, etc.

The Turing 9.2" firmware has no usable rotation command (cmd 125 byte 11
has no effect, verified in phase4e probe). So if a theme's video needs
to be displayed in a different orientation than its source encoding, we
re-encode it once on the host using ffmpeg.

ffmpeg is provided by `imageio-ffmpeg` (a portable binary downloaded
automatically by pip — no manual install required).
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def get_ffmpeg_path() -> Optional[str]:
    """Return path to an ffmpeg binary, preferring imageio-ffmpeg's bundle."""
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    return shutil.which("ffmpeg")


def rotated_cache_path(source_mp4: Path, degrees: int) -> Path:
    """Where we store the pre-rotated copy alongside the source."""
    source_mp4 = Path(source_mp4)
    return source_mp4.with_name(f"{source_mp4.stem}.rot{degrees}{source_mp4.suffix}")


def rotate_mp4(
    source_mp4: Path,
    output_mp4: Path,
    degrees: int,
    crf: int = 16,
    preset: str = "medium",
) -> Path:
    """Rotate `source_mp4` by `degrees` (90 / 180 / 270) into `output_mp4`.

    Returns the output path. Re-encodes H.264 (libx264) with settings tuned
    for the Turing screen's embedded H.264 decoder:

      - profile=baseline, level=3.1 (no B-frames, no advanced features)
      - pix_fmt=yuv420p (8-bit YUV 4:2:0, mandatory for hardware decoders)
      - no B-frames, no scene cut (-bf 0, no-scenecut=1)
      - keyframe every 25 frames (= 1 sec at 25fps) so any frame loss
        recovers within a second
      - faststart so the parser can read moov atom early

    Audio is dropped since the Turing doesn't play sound.
    """
    source_mp4 = Path(source_mp4)
    output_mp4 = Path(output_mp4)
    if degrees not in (90, 180, 270):
        raise ValueError(f"degrees must be 90/180/270, got {degrees}")
    ffmpeg = get_ffmpeg_path()
    if ffmpeg is None:
        raise RuntimeError(
            "ffmpeg not found. Install `pip install imageio-ffmpeg` or system ffmpeg."
        )

    if degrees == 90:
        vf = "transpose=1"
    elif degrees == 270:
        vf = "transpose=2"
    else:  # 180
        vf = "transpose=2,transpose=2"

    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(source_mp4),
        "-vf",
        vf,
        # Codec + quality
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        # Embedded-decoder-friendly profile
        "-profile:v", "baseline",
        "-level", "3.1",
        "-pix_fmt", "yuv420p",
        # No B-frames, frequent keyframes, no scene-cut keyframe shifts
        "-bf", "0",
        "-g", "25",
        "-keyint_min", "25",
        "-sc_threshold", "0",
        "-x264-params", "no-scenecut=1:bframes=0:ref=1:cabac=0",
        # Cap peak bitrate so the decoder doesn't get spikes
        "-maxrate", "3500k",
        "-bufsize", "7000k",
        # Match common BT.709 metadata (matches what most cameras / display
        # decoders expect; avoids subtle color shifts)
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        # Container hint: front-load moov
        "-movflags", "+faststart",
        # No audio
        "-an",
        str(output_mp4),
    ]
    log.info("ffmpeg rotate %d° → %s (baseline / yuv420p / no-bframes)", degrees, output_mp4.name)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # Re-raise with the ffmpeg error
        raise RuntimeError(
            f"ffmpeg rotation failed (exit {proc.returncode}):\n"
            f"  cmd: {' '.join(cmd)}\n"
            f"  stderr: {proc.stderr[-2000:]}"
        )
    return output_mp4


def ensure_rotated(source_mp4: Path, degrees: int) -> Path:
    """If a rotated copy is already cached next to source_mp4, return its path.
    Otherwise produce it (lazy / one-time). degrees=0 returns source unchanged.
    """
    source_mp4 = Path(source_mp4)
    if degrees == 0 or degrees is None:
        return source_mp4
    if degrees not in (90, 180, 270):
        raise ValueError(f"degrees must be 0/90/180/270, got {degrees}")

    out = rotated_cache_path(source_mp4, degrees)
    if out.exists():
        log.info("using cached rotated video: %s", out.name)
        return out

    log.info("first time: rotating %s by %d° → %s", source_mp4.name, degrees, out.name)
    rotate_mp4(source_mp4, out, degrees)
    return out
