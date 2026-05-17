"""Background thread that continuously loops an H.264 stream to the screen.

Lifted from experiments/phase2b_mvp_widget.py with no behavioral changes —
the only difference is taking the H.264 file path as a constructor argument.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from library.lcd.lcd_comm_turing_usb import (
    build_command_packet_header,
    clear_image,
    encrypt_command_packet,
    send_brightness_command,
    send_frame_rate_command,
    write_to_device,
    CMD_PLAY_H264_CHUNK,
    CMD_GET_H264_CHUNK_SIZE,
    CMD_GET_STREAM_STATUS,
    CMD_STOP_STREAM,
)

log = logging.getLogger(__name__)


class StreamingThread(threading.Thread):
    """Loop an H.264 file to the screen until stop() is called.

    Yields between chunks so the widget thread can grab the USB lock and
    push overlays without starving (Python's threading.Lock is not fair —
    the streamer would otherwise re-acquire immediately after release).

    Also paces sends to roughly the video's own bitrate so we don't
    over-fill the screen's decoder buffer (which was triggering the
    flow-control loop and causing observed judder).
    """

    def __init__(
        self,
        dev,
        h264_path: Path,
        usb_lock: threading.Lock,
        brightness: int = 32,
        framerate: int = 25,
        # Feed ~50% faster than the video's CBR so the screen's decoder
        # buffer can refill during the ~200-250ms gaps when the widget
        # thread holds the USB lock to push an overlay PNG.
        target_kbps: int = 3600,
        # With ~400ms natural gap between chunks at 3600 kbps, a small
        # 10ms yield is plenty for the OS scheduler to give the widget
        # thread the lock when it's waiting.
        yield_ms: float = 10.0,
    ):
        super().__init__(daemon=True, name="StreamingThread")
        self.dev = dev
        self.h264_path = Path(h264_path)
        self.usb_lock = usb_lock
        self.brightness = brightness
        self.framerate = framerate
        # Target steady-state feed rate (matches the rotated MP4's CBR).
        self.target_bytes_per_sec = max(1, target_kbps * 1024 // 8)
        # Minimum yield between chunks so the widget thread can win the lock.
        self.yield_s = max(0.0, yield_ms / 1000.0)
        self.stop_event = threading.Event()
        self.chunk_size = 202752
        self.chunks_streamed = 0
        self.usb_errors = 0

    def _safe_write(self, payload):
        with self.usb_lock:
            return write_to_device(self.dev, payload)

    def _simple_cmd(self, opcode: int):
        return self._safe_write(encrypt_command_packet(build_command_packet_header(opcode)))

    def _init_stream(self):
        log.info("[stream] init: cmds 111/112/13/41 + clear + fps=%d", self.framerate)
        self._simple_cmd(111)
        self._simple_cmd(112)
        self._simple_cmd(13)
        with self.usb_lock:
            send_brightness_command(self.dev, self.brightness)
        self._simple_cmd(41)
        with self.usb_lock:
            clear_image(self.dev)
        with self.usb_lock:
            send_frame_rate_command(self.dev, self.framerate)
        resp = self._simple_cmd(CMD_GET_H264_CHUNK_SIZE)
        if resp and len(resp) >= 12:
            negotiated = int.from_bytes(resp[8:12], byteorder="big")
            if 0 < negotiated <= 1024 * 1024:
                self.chunk_size = negotiated
        log.info("[stream] negotiated chunk size %d", self.chunk_size)

    def stop(self):
        self.stop_event.set()

    def run(self):
        try:
            self._init_stream()
        except Exception as exc:
            log.error("[stream] init failed: %s", exc)
            return

        file_size = os.path.getsize(self.h264_path)
        log.info("[stream] looping %s (%d bytes)", self.h264_path.name, file_size)

        try:
            while not self.stop_event.is_set():
                with open(self.h264_path, "rb") as f:
                    while not self.stop_event.is_set():
                        cycle_start = time.monotonic()
                        data = f.read(self.chunk_size)
                        if not data:
                            break
                        chunksize = len(data)
                        is_last = f.tell() == file_size

                        cmd = build_command_packet_header(CMD_PLAY_H264_CHUNK)
                        cmd[8] = (chunksize >> 24) & 0xFF
                        cmd[9] = (chunksize >> 16) & 0xFF
                        cmd[10] = (chunksize >> 8) & 0xFF
                        cmd[11] = chunksize & 0xFF
                        if is_last:
                            cmd[12] = 1
                        resp = self._safe_write(encrypt_command_packet(cmd) + data)
                        if resp is None:
                            self.usb_errors += 1
                        self.chunks_streamed += 1

                        st = self._simple_cmd(CMD_GET_STREAM_STATUS)
                        if st and len(st) > 8 and st[8] > 3:
                            time.sleep(0.05)

                        # Pace the feed to match the video's CBR — prevents
                        # filling the screen's decoder buffer beyond what
                        # it can use, which was eating into flow-control
                        # and producing periodic judder.
                        target_cycle_s = chunksize / self.target_bytes_per_sec
                        elapsed = time.monotonic() - cycle_start
                        # Always sleep at least `yield_s` so the widget
                        # thread can win the USB lock between chunks
                        # (Python locks aren't fair).
                        sleep_s = max(self.yield_s, target_cycle_s - elapsed)
                        if sleep_s > 0:
                            time.sleep(sleep_s)
        finally:
            try:
                self._simple_cmd(CMD_STOP_STREAM)
            except Exception:
                pass
            log.info(
                "[stream] exit: chunks=%d errors=%d", self.chunks_streamed, self.usb_errors
            )
