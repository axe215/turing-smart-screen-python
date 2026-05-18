"""ThemeEngine — orchestrator that runs a ThemeRuntime on the screen.

Lifecycle (Phase 5a):
  - start(): non-blocking. Launches StreamingThread + a daemon widget
    thread, returns immediately. is_running() becomes True.
  - stop(): graceful shutdown. Joins both threads, clears the screen.
  - run(): legacy blocking wrapper for CLI use (calls start, sleeps
    until duration / Ctrl+C, then stop).

A threading.Lock around every write_to_device() serializes USB.
"""
from __future__ import annotations

import logging
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Optional

from library.lcd.lcd_comm_turing_usb import (
    clear_image,
    extract_h264_from_mp4,
    send_image,
    send_jpeg,
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
        font_scale: float = 1.0,
    ):
        self.theme = theme
        self.lcd = lcd
        self.rotate_180 = rotate_180
        # Degrees to physically rotate the source video before extracting
        # H.264 (0/90/180/270). Re-encoded copy is cached next to source.
        self.rotate_video = rotate_video
        self.sources = data_sources or DataSourceRegistry()
        self.renderer = WidgetRenderer(theme, self.sources, screen=screen, font_scale=font_scale)
        self.usb_lock = threading.Lock()
        self.streamer: Optional[StreamingThread] = None
        self.widget_period: float = 1.0
        self._widget_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False
        # Counters surfaced for the UI / CLI
        self.widgets_sent = 0
        self.widget_send_ms_avg = 0.0
        self.started_at: Optional[float] = None

    # ------------------------------------------------------------------
    # H.264 prep
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
        if self.rotate_video:
            mp4_path = ensure_rotated(mp4_path, self.rotate_video)
        h264 = mp4_path.with_suffix(".h264")
        if not h264.exists():
            log.info("extracting H.264 from %s", mp4_path)
            extract_h264_from_mp4(str(mp4_path))
        return h264

    # ------------------------------------------------------------------
    # Public lifecycle API
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        return self._running

    def start(self, widget_period: float = 1.0, stream_warmup: float = 2.0) -> None:
        """Non-blocking start. Returns once threads are live and the
        stream has had `stream_warmup` seconds to settle."""
        if self._running:
            raise RuntimeError("ThemeEngine already running; stop() first")

        log.info(
            "ThemeEngine.start: theme=%s canvas=%dx%d widgets=%d rotate_180=%s",
            self.theme.name,
            self.theme.canvas.width,
            self.theme.canvas.height,
            len(self.theme.widgets),
            self.rotate_180,
        )
        self.widget_period = max(0.05, float(widget_period))
        self.widgets_sent = 0
        self.widget_send_ms_avg = 0.0
        self._stop_event.clear()

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

        self._widget_thread = threading.Thread(
            target=self._widget_loop, daemon=True, name="WidgetThread"
        )
        self._widget_thread.start()
        self.started_at = time.monotonic()
        self._running = True

    def stop(self) -> None:
        """Graceful shutdown. Idempotent: safe to call when not running."""
        if not self._running and self.streamer is None and self._widget_thread is None:
            return
        log.info("ThemeEngine.stop")
        self._stop_event.set()

        if self._widget_thread is not None:
            self._widget_thread.join(timeout=5.0)
            if self._widget_thread.is_alive():
                log.warning("WidgetThread did not exit cleanly")
            self._widget_thread = None

        if self.streamer is not None:
            self.streamer.stop()
            self.streamer.join(timeout=5.0)
            if self.streamer.is_alive():
                log.warning("StreamingThread did not exit cleanly")
            self.streamer = None

        # Clear the panel so the next theme starts from a clean slate.
        # Don't crash if the screen has gone away in the meantime.
        try:
            with self.usb_lock:
                clear_image(self.lcd.dev)
        except Exception as exc:
            log.debug("clear_image on stop failed (ignored): %s", exc)

        self._running = False

    # ------------------------------------------------------------------
    # Widget render loop (runs in WidgetThread)
    # ------------------------------------------------------------------

    def _widget_loop(self) -> None:
        send_ms_total = 0.0
        while not self._stop_event.is_set():
            cycle_start = time.monotonic()
            try:
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
            except Exception as exc:
                log.warning("widget render failed: %s", exc)

            elapsed = time.monotonic() - cycle_start
            wait = self.widget_period - elapsed
            if wait > 0:
                # event.wait() so stop() takes effect immediately rather
                # than after a full widget cycle.
                if self._stop_event.wait(wait):
                    break

    # ------------------------------------------------------------------
    # PNG/JPEG encoding helper (unchanged from prior impl)
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_payload(img):
        """Encode the overlay image to PNG (fast) or JPEG (fallback)."""
        buf = BytesIO()
        img.save(buf, format="PNG", compress_level=3)
        png_bytes = buf.getvalue()
        if len(png_bytes) <= MAX_IMAGE_PAYLOAD_DEFAULT:
            return png_bytes, "png"
        from library.lcd.lcd_comm_turing_usb import _encode_jpeg_under_limit  # local
        jpg_bytes = _encode_jpeg_under_limit(
            img, max_bytes=MAX_IMAGE_PAYLOAD_DEFAULT, quality=90, subsampling=-1
        )
        return jpg_bytes, "jpeg"

    # ------------------------------------------------------------------
    # Status snapshot (for UI status panels)
    # ------------------------------------------------------------------

    def status(self) -> dict:
        return {
            "running": self._running,
            "theme": self.theme.name,
            "uptime_sec": (time.monotonic() - self.started_at) if self.started_at else 0.0,
            "widgets_sent": self.widgets_sent,
            "widget_send_ms_avg": round(self.widget_send_ms_avg, 1),
            "stream_chunks": self.streamer.chunks_streamed if self.streamer else 0,
            "stream_errors": self.streamer.usb_errors if self.streamer else 0,
            "rotate_180": self.rotate_180,
            "rotate_video": self.rotate_video,
            "font_scale": self.renderer.font_scale,
            "widget_period": self.widget_period,
        }

    # ------------------------------------------------------------------
    # Blocking convenience entrypoint (legacy CLI compatibility)
    # ------------------------------------------------------------------

    def run(
        self,
        duration: Optional[float] = None,
        widget_period: float = 1.0,
        stream_warmup: float = 2.0,
    ) -> None:
        """Block until `duration` seconds pass or Ctrl+C, then stop()."""
        self.start(widget_period=widget_period, stream_warmup=stream_warmup)
        try:
            if duration is not None:
                # Sleep in small slices so Ctrl+C is responsive
                end = time.monotonic() + duration
                while time.monotonic() < end and self._running:
                    time.sleep(0.2)
            else:
                while self._running:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            log.info("ThemeEngine: interrupted")
        finally:
            self.stop()
