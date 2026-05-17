"""Probe firmware rotation via cmd 125 (save_settings).

The Turing 9.2" firmware has a `rotation` byte in the save_settings packet
(cmd 125 byte 11). Values are not documented — likely 0/1/2/3 mapping to
0°/90°/180°/270°, but the actual mapping for the 9.2" is unknown.

This script lets us probe what each rotation value does, watching both
the video and a test overlay. After cycling, we reset to rotation=0.

⚠️ WARNING: cmd 125 is persistent (saves to firmware flash). If you cancel
mid-test, the screen may keep rotation X on next reboot. Re-run with
--rotation 0 to reset, or open the stock UsbMonitorL.exe (which resets
on startup).

Usage:

    # Try a specific rotation value, hold for 10s, then exit
    python experiments/phase4e_rotation_probe.py --rotation 2

    # Cycle 0 → 1 → 2 → 3 with 8s pauses, then reset to 0
    python experiments/phase4e_rotation_probe.py --cycle

    # With a video playing as background to observe rotation effect on it
    python experiments/phase4e_rotation_probe.py --rotation 2 \\
        --video rei/eva.rei/video/Finalrei.mp421103329.mp4
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from library.lcd.lcd_comm_turing_usb import (  # noqa: E402
    LcdCommTuringUSB,
    extract_h264_from_mp4,
    send_brightness_command,
    send_save_settings_command,
    send_pil_image_auto,
)
from library.theme_engine.streaming import StreamingThread  # noqa: E402

from PIL import Image, ImageDraw, ImageFont


SCREEN_W, SCREEN_H = 462, 1920


def build_marker_overlay() -> Image.Image:
    """Bright marker corners so we can identify orientation visually."""
    img = Image.new("RGBA", (SCREEN_W, SCREEN_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Corners
    draw.rectangle([0, 0, 80, 80], fill=(0, 255, 0, 255))            # green TOP-LEFT (portrait native)
    draw.rectangle([SCREEN_W - 80, 0, SCREEN_W, 80], fill=(255, 255, 0, 255))            # yellow TOP-RIGHT
    draw.rectangle([0, SCREEN_H - 80, 80, SCREEN_H], fill=(0, 0, 255, 255))              # blue BOTTOM-LEFT
    draw.rectangle([SCREEN_W - 80, SCREEN_H - 80, SCREEN_W, SCREEN_H], fill=(255, 0, 255, 255))  # magenta BOTTOM-RIGHT
    try:
        font = ImageFont.truetype("arial.ttf", 80)
    except OSError:
        font = ImageFont.load_default()
    draw.text((SCREEN_W // 2 - 40, 200), "TOP", font=font, fill=(255, 255, 255, 255),
              stroke_width=3, stroke_fill=(0, 0, 0, 255))
    draw.text((SCREEN_W // 2 - 90, SCREEN_H - 280), "BOTTOM", font=font, fill=(255, 255, 255, 255),
              stroke_width=3, stroke_fill=(0, 0, 0, 255))
    return img


def set_rotation(lcd, rotation: int, brightness: int):
    print(f"\n  → cmd 125: rotation={rotation}, brightness={brightness}")
    send_save_settings_command(
        lcd.dev,
        brightness=brightness,
        startup=0,
        reserved=0,
        rotation=rotation,
        sleep=0,
        offline=0,
    )
    # cmd 14 to refresh current brightness regardless of what save_settings set
    time.sleep(0.3)
    send_brightness_command(lcd.dev, int(brightness / 100 * 102))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--rotation", type=int, choices=[0, 1, 2, 3], default=None,
                   help="Set rotation to this value (0-3) and hold")
    p.add_argument("--cycle", action="store_true",
                   help="Cycle 0→1→2→3 with --hold seconds each, then reset to 0")
    p.add_argument("--hold", type=float, default=8.0, help="Seconds to hold each value (default 8)")
    p.add_argument("--brightness", type=int, default=50,
                   help="Brightness to set with cmd 125 (default 50). Avoids dark screen.")
    p.add_argument("--video", type=Path, default=None,
                   help="Optional MP4 to stream in background so we can see if video rotates too")
    p.add_argument("--reset", action="store_true",
                   help="Just set rotation back to 0 and exit (emergency)")
    args = p.parse_args()

    if args.rotation is None and not args.cycle and not args.reset:
        p.error("must specify --rotation N, --cycle, or --reset")

    print("Connecting to screen...")
    lcd = LcdCommTuringUSB()
    print(f"  PID=0x{lcd.dev_pid:04x}, native portrait {lcd.display_width}x{lcd.display_height}")
    lcd.InitializeComm()

    if args.reset:
        set_rotation(lcd, 0, args.brightness)
        print("\nReset to rotation=0, brightness=", args.brightness)
        return 0

    streamer = None
    usb_lock = threading.Lock()
    if args.video is not None and args.video.exists():
        print(f"Extracting H.264 from {args.video}...")
        h264 = Path(extract_h264_from_mp4(str(args.video)))
        streamer = StreamingThread(lcd.dev, h264, usb_lock)
        streamer.start()
        print("Streaming started; warmup 2s")
        time.sleep(2)

    marker = build_marker_overlay()

    def push_marker():
        with usb_lock:
            send_pil_image_auto(lcd.dev, marker)

    try:
        if args.cycle:
            for r in (0, 1, 2, 3):
                set_rotation(lcd, r, args.brightness)
                push_marker()
                print(f"  Look at screen for {args.hold}s — note which corner each color is in.")
                time.sleep(args.hold)
            print("\nResetting to rotation=0")
            set_rotation(lcd, 0, args.brightness)
            push_marker()
        else:
            set_rotation(lcd, args.rotation, args.brightness)
            push_marker()
            print(f"  Holding rotation={args.rotation} for {args.hold}s — observe.")
            time.sleep(args.hold)
            # If a one-shot test, also reset on the way out
            print(f"\nResetting to rotation=0 before exit")
            set_rotation(lcd, 0, args.brightness)
            push_marker()
    except KeyboardInterrupt:
        print("\nInterrupted — resetting rotation to 0")
        try:
            set_rotation(lcd, 0, args.brightness)
        except Exception:
            pass
    finally:
        if streamer is not None:
            streamer.stop()
            streamer.join(timeout=3)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
