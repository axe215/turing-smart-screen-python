"""ThemeEngine — orchestrator that runs a ThemeRuntime on the screen.

Two threads (lifted from Phase 2b proven architecture):
  - StreamingThread  loops H.264 to the screen, never stops until stop()
  - main thread      every `widget_period`s, render widgets + push as PNG

A threading.Lock around every write_to_device() serializes USB.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from io import BytesIO

from library.lcd.lcd_comm_turing_usb import (
    extract_h264_from_mp4,
    send_image,
    send_jpeg,
    send_pil_image_auto,
    MAX_IMAGE_PAYLOAD_DEFAULT,
)

from .data_sources import DataSourceRegistry
from .renderer import WidgetRenderer
from .runtime import ThemeRuntime
from .streaming import StreamingThread
from .video_utils import ensure_rotated

log = logging.getLogger(__name__)


class ThemeEngine:
    def __init__(
        self,
        theme: ThemeRuntime,
        lcd,
        screen: str = "9.2",
        data_sources: Optional[DataSourceRegistry] = None,
        rotate_180: bool = False,
        rotate_video: int = 0,
    ):
        self.theme = theme
        self.lcd = lcd
        self.rotate_180 = rotate_180
        # Degrees to physically rotate the source video before extracting
        # H.264 (0/90/180/270). Re-encoded copy is cached next to source.
        self.rotate_video = rotate_video
        self.sources = data_sources or DataSourceRegistry()
        self.renderer = WidgetRenderer(theme, self.sources, screen=screen)
        self.usb_lock = threading.Lock()
        self.streamer: Optional[StreamingThread] = None
        # Counters surfaced for the CLI to log
        self.widgets_sent = 0
        self.widget_send_ms_avg = 0.0

    # ------------------------------------------------------------------

    def _ensure_h264(self) -> Optional[Path]:
        """Extract the theme's MP4 to H.264 once and return that path.
        If rotate_video is set, the MP4 is pre-rotated (cached) first.
        Returns None if the theme has no video."""
        if self.theme.video is None:
            return None
        mp4_path = self.theme.video_path
        if mp4_path is None or not mp4_path.exists():
            log.warning("video file %s not found", mp4_path)
            return None
        # Pre-rotate the MP4 once (re-encoded copy cached next to source)
        if self.rotate_video:
            mp4_path = ensure_rotated(mp4_path, self.rotate_video)
        h264 = mp4_path.with_suffix(".h264")
        if not h264.exists():
            log.info("extracting H.264 from %s", mp4_path)
            extract_h264_from_mp4(str(mp4_path))
        return h264

    # ------------------------------------------------------------------

    def run(
        self,
        duration: Optional[float] = None,
        widget_period: float = 1.0,
        stream_warmup: float = 2.0,
    ) -> None:
        """Run until `duration` seconds pass or Ctrl+C.

        If duration is None, run forever (until KeyboardInterrupt).
        """
        log.info(
            "ThemeEngine: theme=%s canvas=%dx%d widgets=%d rotate_180=%s",
            self.theme.name,
            self.theme.canvas.width,
            self.theme.canvas.height,
            len(self.theme.widgets),
            self.rotate_180,
        )

        h264_path = self._ensure_h264()
        if h264_path is not None:
            self.streamer = StreamingThread(
                self.lcd.dev,
                h264_path,
                self.usb_lock,
                framerate=self.theme.video.framerate if self.theme.video else 25,
            )
            self.streamer.start()
            log.info("ThemeEngine: streaming started; warmup %.1fs", stream_warmup)
            time.sleep(stream_warmup)

        end_time = time.monotonic() + duration if duration is not None else float("inf")
        send_ms_total = 0.0
        try:
            while time.monotonic() < end_time:
                cycle = time.monotonic()

                # Render + encode OUTSIDE the USB lock so the streaming
                # thread keeps feeding the screen with H.264 chunks in
                # parallel. The lock is held only for the actual USB
                # write, which is ~30-50ms (vs ~270ms when encoding was
                # inside the lock — that caused the decoder to underrun
                # and produced motion judder/blur).
                t_render = time.monotonic()
                img = self.renderer.render_frame(rotate_180=self.rotate_180)
                render_ms = (time.monotonic() - t_render) * 1000

                t_encode = time.monotonic()
                payload, fmt = self._encode_payload(img)
                encode_ms = (time.monotonic() - t_encode) * 1000

                t_send = time.monotonic()
                with self.usb_lock:
                    if fmt == "png":
                        send_image(self.lcd.dev, payload)
                    else:
                        send_jpeg(self.lcd.dev, payload)
                send_ms = (time.monotonic() - t_send) * 1000

                self.widgets_sent += 1
                send_ms_total += send_ms
                self.widget_send_ms_avg = send_ms_total / self.widgets_sent

                if self.widgets_sent <= 3 or self.widgets_sent % 10 == 0:
                    log.info(
                        "[widget #%d] render=%.0fms enc=%.0fms send=%.0fms (lock-held) %s",
                        self.widgets_sent,
                        render_ms,
                        encode_ms,
                        send_ms,
                        f"stream_chunks={self.streamer.chunks_streamed}" if self.streamer else "(no video)",
                    )

                elapsed = time.monotonic() - cycle
                time.sleep(max(0.0, widget_period - elapsed))
        except KeyboardInterrupt:
            log.info("ThemeEngine: interrupted")
        finally:
            self.stop()

    @staticmethod
    def _encode_payload(img):
        """Encode the overlay image to PNG (fast) or JPEG (fallback).

        Returns (bytes, 'png' | 'jpeg'). compress_level=3 is far faster
        than the default 9 and produces near-identical sizes for the
        mostly-transparent overlay PNGs we use.
        """
        buf = BytesIO()
        img.save(buf, format="PNG", compress_level=3)
        png_bytes = buf.getvalue()
        if len(png_bytes) <= MAX_IMAGE_PAYLOAD_DEFAULT:
            return png_bytes, "png"
        # Fall back to JPEG when PNG exceeds the per-frame limit
        from library.lcd.lcd_comm_turing_usb import _encode_jpeg_under_limit  # local import
        jpg_bytes = _encode_jpeg_under_limit(
            img, max_bytes=MAX_IMAGE_PAYLOAD_DEFAULT, quality=90, subsampling=-1
        )
        return jpg_bytes, "jpeg"

    def stop(self):
        if self.streamer is not None:
            self.streamer.stop()
            self.streamer.join(timeout=5.0)
            if self.streamer.is_alive():
                log.warning("StreamingThread did not exit cleanly")
