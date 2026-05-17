"""Phase 2b MVP: stream a video as the background and overlay live widgets.

Validates the two-thread architecture proven by Phase 2 (scenario A):
  - Background thread: continuously streams H.264 chunks (cmd 121)
  - Main thread: renders CPU/RAM/clock widgets to a 462x1920 RGBA PNG
                 and pushes via cmd 102 every N seconds
  - threading.Lock around every write_to_device() call serializes USB

This is the architectural skeleton that Phase 4 (.turtheme-driven theme
engine) will build on. If this runs cleanly for 60 seconds, we're golden.

Run from repo root, venv active:

  python experiments/phase2b_mvp_widget.py rei/eva.rei/video/Finalrei.mp421103329.mp4

Useful options:
  --duration N          run for N seconds (default: 60)
  --widget-period S     widget update period in seconds (default: 1.0)
  --rotate-180          rotate widget overlay 180° (visual orientation match)

NOTE on rotation: this MVP does NOT rotate the H.264 stream. Whatever
orientation your Phase 1 test showed (likely 180° from how you have the
screen physically mounted) is how the video will appear here. The widgets
will appear in the same coordinate frame as the video unless --rotate-180.
We'll resolve rotation properly in Phase 4 (probably via cmd 125 HW
rotation or by pre-processing the H.264 source).
"""

from __future__ import annotations

import argparse
import logging
import os
import struct
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import psutil
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from library.lcd.lcd_comm_turing_usb import (  # noqa: E402
    LcdCommTuringUSB,
    extract_h264_from_mp4,
    build_command_packet_header,
    encrypt_command_packet,
    write_to_device,
    send_brightness_command,
    send_frame_rate_command,
    send_pil_image_auto,
    clear_image,
    CMD_PLAY_H264_CHUNK,
    CMD_GET_H264_CHUNK_SIZE,
    CMD_GET_STREAM_STATUS,
    CMD_STOP_STREAM,
)


# Turing 9.2" native portrait dimensions (not the 8.8"-themed 480!)
NATIVE_W = 462
NATIVE_H = 1920


def _load_font(size: int) -> ImageFont.ImageFont:
    for name in ("arial.ttf", "DejaVuSans.ttf", "Arial.ttf", "C:\\Windows\\Fonts\\arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_widget_overlay(rotate_180: bool) -> Image.Image:
    """Build a 462x1920 RGBA image with CPU%, RAM%, and a clock.

    Transparent everywhere except a semi-opaque panel near one short
    edge — so the video shows through the rest.
    """
    img = Image.new("RGBA", (NATIVE_W, NATIVE_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory().percent
    clock = datetime.now().strftime("%H:%M:%S")

    big_font = _load_font(76)
    label_font = _load_font(34)
    clock_font = _load_font(54)

    # Panel near "top" in native portrait (one short edge)
    panel_x0, panel_y0 = 16, 60
    panel_x1, panel_y1 = NATIVE_W - 16, 520
    draw.rectangle([panel_x0, panel_y0, panel_x1, panel_y1], fill=(0, 0, 0, 180))

    pad = 32
    draw.text((panel_x0 + pad, panel_y0 + 14), "CPU", fill=(180, 180, 180), font=label_font)
    draw.text((panel_x0 + pad, panel_y0 + 52), f"{cpu:5.1f}%", fill=(0, 255, 120), font=big_font)
    draw.text((panel_x0 + pad, panel_y0 + 168), "RAM", fill=(180, 180, 180), font=label_font)
    draw.text((panel_x0 + pad, panel_y0 + 206), f"{ram:5.1f}%", fill=(120, 200, 255), font=big_font)
    draw.text((panel_x0 + pad, panel_y0 + 340), clock, fill=(255, 220, 80), font=clock_font)

    if rotate_180:
        img = img.transpose(Image.Transpose.ROTATE_180)
    return img


class StreamingThread(threading.Thread):
    """Background thread that continuously loops an H.264 file to the screen.

    Uses the same init sequence and chunk loop as upstream's send_video().
    All write_to_device() calls are guarded by `usb_lock`.
    """

    def __init__(self, dev, h264_path: Path, usb_lock: threading.Lock):
        super().__init__(daemon=True, name="StreamingThread")
        self.dev = dev
        self.h264_path = h264_path
        self.usb_lock = usb_lock
        self.stop_event = threading.Event()
        self.chunk_size = 202752
        self.chunks_streamed = 0
        self.usb_errors = 0

    def _safe_write(self, payload):
        with self.usb_lock:
            return write_to_device(self.dev, payload)

    def _send_simple_cmd(self, opcode: int):
        return self._safe_write(encrypt_command_packet(build_command_packet_header(opcode)))

    def _init_stream(self):
        logging.info("[stream] sending video-mode init (111/112/13/41)")
        self._send_simple_cmd(111)
        self._send_simple_cmd(112)
        self._send_simple_cmd(13)
        with self.usb_lock:
            send_brightness_command(self.dev, 32)
        self._send_simple_cmd(41)
        with self.usb_lock:
            clear_image(self.dev)
        with self.usb_lock:
            send_frame_rate_command(self.dev, 25)
        resp = self._send_simple_cmd(CMD_GET_H264_CHUNK_SIZE)
        if resp and len(resp) >= 12:
            negotiated = int.from_bytes(resp[8:12], byteorder="big")
            if 0 < negotiated <= 1024 * 1024:
                self.chunk_size = negotiated
        logging.info(f"[stream] negotiated chunk size: {self.chunk_size}")

    def stop(self):
        self.stop_event.set()

    def run(self):
        try:
            self._init_stream()
        except Exception as exc:
            logging.error(f"[stream] init failed: {exc}")
            return

        file_size = os.path.getsize(self.h264_path)
        logging.info(f"[stream] streaming {self.h264_path.name} ({file_size} bytes)")

        try:
            while not self.stop_event.is_set():
                with open(self.h264_path, "rb") as f:
                    while not self.stop_event.is_set():
                        data = f.read(self.chunk_size)
                        if not data:
                            break

                        chunksize = len(data)
                        is_last = f.tell() == file_size

                        cmd_packet = build_command_packet_header(CMD_PLAY_H264_CHUNK)
                        cmd_packet[8] = (chunksize >> 24) & 0xFF
                        cmd_packet[9] = (chunksize >> 16) & 0xFF
                        cmd_packet[10] = (chunksize >> 8) & 0xFF
                        cmd_packet[11] = chunksize & 0xFF
                        if is_last:
                            cmd_packet[12] = 1

                        full_payload = encrypt_command_packet(cmd_packet) + data
                        resp = self._safe_write(full_payload)
                        if resp is None:
                            self.usb_errors += 1

                        self.chunks_streamed += 1

                        # Light backpressure
                        st = self._send_simple_cmd(CMD_GET_STREAM_STATUS)
                        if st and len(st) > 8 and st[8] > 3:
                            time.sleep(0.05)
        finally:
            try:
                self._send_simple_cmd(CMD_STOP_STREAM)
            except Exception:
                pass
            logging.info(
                f"[stream] exiting. chunks={self.chunks_streamed} usb_errors={self.usb_errors}"
            )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("video", help="Path to MP4 file")
    p.add_argument("--duration", type=int, default=60, help="Total runtime in seconds (default: 60)")
    p.add_argument(
        "--widget-period",
        type=float,
        default=1.0,
        help="Seconds between widget updates (default: 1.0)",
    )
    p.add_argument(
        "--rotate-180",
        action="store_true",
        help="Rotate the widget overlay 180° (use if you want widgets to match a flipped screen)",
    )
    args = p.parse_args()

    video_path = Path(args.video).expanduser().resolve()
    if not video_path.exists():
        print(f"ERROR: video not found: {video_path}", file=sys.stderr)
        return 1

    print("== Phase 2b MVP: video bg + live widgets ==")
    print(f"Video         : {video_path}")
    print(f"Duration      : {args.duration}s")
    print(f"Widget period : {args.widget_period:.1f}s")
    print(f"Rotate 180    : {args.rotate_180}\n")

    h264_path = Path(extract_h264_from_mp4(str(video_path)))

    print("Connecting to screen...")
    lcd = LcdCommTuringUSB()
    print(f"  PID=0x{lcd.dev_pid:04x}, native portrait {lcd.display_width}x{lcd.display_height}")
    lcd.InitializeComm()

    usb_lock = threading.Lock()
    streamer = StreamingThread(lcd.dev, h264_path, usb_lock)

    print("\nStarting stream thread...")
    streamer.start()
    time.sleep(2.0)  # let the stream begin before we start drawing overlays

    print(f"\nWidget loop will run for {args.duration}s. Watch the screen.")
    print(f"Expected: Rei video looping + CPU/RAM/clock panel updating every {args.widget_period:.1f}s.\n")

    end_time = time.monotonic() + args.duration
    widget_count = 0
    psutil.cpu_percent(interval=None)  # prime; first call returns 0.0

    try:
        while time.monotonic() < end_time:
            cycle_start = time.monotonic()
            img = render_widget_overlay(rotate_180=args.rotate_180)
            send_t0 = time.monotonic()
            with usb_lock:
                send_pil_image_auto(lcd.dev, img)
            send_ms = (time.monotonic() - send_t0) * 1000
            widget_count += 1

            if widget_count % 5 == 0 or widget_count <= 3:
                logging.info(
                    f"[widget #{widget_count}] send={send_ms:.0f}ms "
                    f"stream_chunks={streamer.chunks_streamed} "
                    f"usb_errors={streamer.usb_errors}"
                )

            elapsed = time.monotonic() - cycle_start
            sleep_for = max(0.0, args.widget_period - elapsed)
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        print("\nInterrupted by user.")

    print(f"\nStopping. Final stats:")
    print(f"  widgets sent  : {widget_count}")
    print(f"  stream chunks : {streamer.chunks_streamed}")
    print(f"  USB errors    : {streamer.usb_errors}")

    streamer.stop()
    streamer.join(timeout=5.0)
    if streamer.is_alive():
        print("WARNING: streamer thread did not exit within 5s")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
