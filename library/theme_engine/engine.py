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

from library.lcd.lcd_comm_turing_usb import (
    extract_h264_from_mp4,
    send_pil_image_auto,
)

from .data_sources import DataSourceRegistry
from .renderer import WidgetRenderer
from .runtime import ThemeRuntime
from .streaming import StreamingThread

log = logging.getLogger(__name__)


class ThemeEngine:
    def __init__(
        self,
        theme: ThemeRuntime,
        lcd,
        screen: str = "9.2",
        data_sources: Optional[DataSourceRegistry] = None,
        rotate_180: bool = False,
    ):
        self.theme = theme
        self.lcd = lcd
        self.rotate_180 = rotate_180
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
        Returns None if the theme has no video."""
        if self.theme.video is None:
            return None
        mp4_path = self.theme.video_path
        if mp4_path is None or not mp4_path.exists():
            log.warning("video file %s not found", mp4_path)
            return None
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
                img = self.renderer.render_frame(rotate_180=self.rotate_180)
                t0 = time.monotonic()
                with self.usb_lock:
                    send_pil_image_auto(self.lcd.dev, img)
                send_ms = (time.monotonic() - t0) * 1000
                send_ms_total += send_ms
                self.widgets_sent += 1
                self.widget_send_ms_avg = send_ms_total / self.widgets_sent

                if self.widgets_sent <= 3 or self.widgets_sent % 10 == 0:
                    log.info(
                        "[widget #%d] send=%.0fms avg=%.0fms %s",
                        self.widgets_sent,
                        send_ms,
                        self.widget_send_ms_avg,
                        f"stream_chunks={self.streamer.chunks_streamed}" if self.streamer else "(no video)",
                    )

                elapsed = time.monotonic() - cycle
                time.sleep(max(0.0, widget_period - elapsed))
        except KeyboardInterrupt:
            log.info("ThemeEngine: interrupted")
        finally:
            self.stop()

    def stop(self):
        if self.streamer is not None:
            self.streamer.stop()
            self.streamer.join(timeout=5.0)
            if self.streamer.is_alive():
                log.warning("StreamingThread did not exit cleanly")
