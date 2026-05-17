"""Phase 2: probe widget-over-video behavior on Turing 9.2".

We interleave H.264 chunks (cmd 121) and full-screen PNG overlays (cmd 102)
to the same screen and watch what happens. There are three possible
firmware behaviors:

  A) Overlay appears ON TOP of the video, video keeps playing.
     → ideal: we just need a streaming thread + a widget thread.

  B) Overlay REPLACES the framebuffer; video stops or freezes.
     → we need dedicated overlay opcodes (rev_c has 0xCA/0xCC for
       sprite-on-video, but those aren't exposed for the 9.2" class yet).
     → next step: USBPcap sniff of stock UsbMonitorL to reverse-engineer.

  C) Garbled output, no response, or repeating artifacts.
     → firmware quirk; needs more diagnostic work.

Run from repo root, venv active:

  python experiments/phase2_overlay_probe.py rei/eva.rei/video/Finalrei.mp421103329.mp4

The script will:
  1. Initialize a video stream (cmds 111/112/13/41 + brightness + clear + fps)
  2. Loop H.264 chunks
  3. Every N chunks, send a full-screen PNG with a visible overlay box
  4. Stop the stream after a fixed overlay count
"""

from __future__ import annotations

import argparse
import logging
import os
import struct
import sys
import time
from pathlib import Path

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


SCREEN_W = 480  # native portrait width of 9.2" (close enough; actual 462)
SCREEN_H = 1920


def _load_font(size: int) -> ImageFont.ImageFont:
    for name in ("arial.ttf", "DejaVuSans.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def build_overlay_image(n: int, background: str) -> Image.Image:
    """Build a full-screen 480x1920 RGBA image with a highly visible widget.

    Background is the entire screen behind the widget — transparent lets us
    test whether the firmware blends with the video below, opaque lets us
    test whether video stops.
    """
    bg_colors = {
        "transparent": (0, 0, 0, 0),
        "black": (0, 0, 0, 255),
        "blue": (0, 0, 255, 255),
        "magenta": (255, 0, 255, 255),
    }
    img = Image.new("RGBA", (SCREEN_W, SCREEN_H), bg_colors[background])
    draw = ImageDraw.Draw(img)

    # Big red semi-transparent banner near one short edge (so it's obvious
    # even with rotation). 480 wide.
    draw.rectangle([0, 60, SCREEN_W, 360], fill=(220, 30, 30, 230))
    big_font = _load_font(90)
    small_font = _load_font(40)
    draw.text((30, 90), "OVERLAY", fill=(255, 255, 255, 255), font=big_font)
    draw.text((30, 220), f"#{n}", fill=(255, 255, 255, 255), font=big_font)
    draw.text((30, 320), f"bg={background}", fill=(255, 255, 0, 255), font=small_font)

    # Corner markers so we can identify orientation on screen
    draw.rectangle([0, 0, 60, 60], fill=(0, 255, 0, 255))           # green - top-left in portrait
    draw.rectangle([SCREEN_W - 60, 0, SCREEN_W, 60], fill=(255, 255, 0, 255))   # yellow - top-right
    draw.rectangle([0, SCREEN_H - 60, 60, SCREEN_H], fill=(0, 0, 255, 255))     # blue - bottom-left
    draw.rectangle([SCREEN_W - 60, SCREEN_H - 60, SCREEN_W, SCREEN_H], fill=(255, 0, 255, 255))  # magenta - bottom-right
    return img


def stream_with_overlays(
    dev,
    h264_path: Path,
    overlay_every_n_chunks: int,
    total_overlays: int,
    background: str,
):
    # Video session init (same sequence as send_video())
    print("Initializing video stream (cmds 111/112/13/41 + clear + framerate)...")
    write_to_device(dev, encrypt_command_packet(build_command_packet_header(111)))
    write_to_device(dev, encrypt_command_packet(build_command_packet_header(112)))
    write_to_device(dev, encrypt_command_packet(build_command_packet_header(13)))
    send_brightness_command(dev, 32)
    write_to_device(dev, encrypt_command_packet(build_command_packet_header(41)))
    clear_image(dev)
    send_frame_rate_command(dev, 25)

    # Negotiate H.264 chunk size
    resp = write_to_device(dev, encrypt_command_packet(build_command_packet_header(CMD_GET_H264_CHUNK_SIZE)))
    chunk_size = 202752
    if resp and len(resp) >= 12:
        negotiated = int.from_bytes(resp[8:12], byteorder="big")
        if 0 < negotiated <= 1024 * 1024:
            chunk_size = negotiated
    print(f"Negotiated H.264 chunk size: {chunk_size} bytes")

    file_size = os.path.getsize(h264_path)
    print(f"H.264 file size: {file_size} bytes ({file_size // chunk_size + 1} chunks per loop)")

    chunk_idx = 0
    overlay_count = 0

    print(f"Starting stream. Will inject overlay every {overlay_every_n_chunks} chunks.")
    print(f"Press Ctrl+C to stop early.\n")

    try:
        with open(h264_path, "rb") as f:
            while overlay_count < total_overlays:
                data = f.read(chunk_size)
                if not data:
                    f.seek(0)
                    print("(video file looped)")
                    continue

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
                write_to_device(dev, full_payload)
                chunk_idx += 1

                # Light backpressure
                st = write_to_device(
                    dev, encrypt_command_packet(build_command_packet_header(CMD_GET_STREAM_STATUS))
                )
                if st and len(st) > 8 and st[8] > 3:
                    time.sleep(0.05)

                if chunk_idx % overlay_every_n_chunks == 0:
                    overlay_count += 1
                    img = build_overlay_image(overlay_count, background)
                    print(f"\n>>> [chunk #{chunk_idx}] Sending OVERLAY #{overlay_count} ({background} bg)")
                    print(f"    LOOK AT THE SCREEN NOW. Pausing 3s after send to observe.")
                    t0 = time.monotonic()
                    send_pil_image_auto(dev, img)
                    dt = time.monotonic() - t0
                    print(f"    Overlay PNG sent in {dt * 1000:.0f}ms.")
                    time.sleep(3)
                    print(f"    Resuming stream...\n")
    finally:
        print("\nSending CMD_STOP_STREAM (123)...")
        write_to_device(dev, encrypt_command_packet(build_command_packet_header(CMD_STOP_STREAM)))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("video", help="Path to MP4 file")
    p.add_argument(
        "--overlay-interval",
        type=int,
        default=5,
        help="Inject overlay every N H.264 chunks (default: 5)",
    )
    p.add_argument(
        "--overlay-count",
        type=int,
        default=5,
        help="Stop after this many overlays sent (default: 5)",
    )
    p.add_argument(
        "--background",
        choices=["transparent", "black", "blue", "magenta"],
        default="transparent",
        help=(
            "Overlay background fill: transparent (test if firmware blends), "
            "black/blue/magenta (test if PNG replaces framebuffer). Default: transparent."
        ),
    )
    args = p.parse_args()

    video_path = Path(args.video).expanduser().resolve()
    if not video_path.exists():
        print(f"ERROR: video not found: {video_path}", file=sys.stderr)
        return 1

    print("== Phase 2: widget-over-video probe ==")
    print(f"Video           : {video_path}")
    print(f"Overlay every   : {args.overlay_interval} chunks")
    print(f"Overlay count   : {args.overlay_count}")
    print(f"Overlay bg      : {args.background}\n")

    h264_path = extract_h264_from_mp4(str(video_path))
    print(f"H.264 source: {h264_path}")

    print("\nConnecting to screen...")
    lcd = LcdCommTuringUSB()
    print(f"  PID=0x{lcd.dev_pid:04x}, native portrait size {lcd.display_width}x{lcd.display_height}")
    lcd.InitializeComm()

    try:
        stream_with_overlays(
            lcd.dev,
            Path(h264_path),
            overlay_every_n_chunks=args.overlay_interval,
            total_overlays=args.overlay_count,
            background=args.background,
        )
    except KeyboardInterrupt:
        print("\nInterrupted. Sending CMD_STOP_STREAM...")
        write_to_device(
            lcd.dev, encrypt_command_packet(build_command_packet_header(CMD_STOP_STREAM))
        )

    print("\n========================================================")
    print("OBSERVATION CHEAT-SHEET — pick the outcome you saw:")
    print("  A) Video kept playing AND red banner appeared on top")
    print("     → IDEAL. Firmware composites natively. Easy Phase 4.")
    print("  B) Video froze/stopped. Red banner visible alone.")
    print("     → Need to find overlay opcodes (0xCA/0xCC?)")
    print("  C) Glitch/garbage/nothing happened")
    print("     → Need to look at USB response bytes more carefully")
    print("========================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
