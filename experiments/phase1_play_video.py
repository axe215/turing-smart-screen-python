"""Phase 1: upload an MP4 to the Turing 9.2" screen's on-device storage and start playback.

Goal of this experiment: verify that the screen can play H.264 video directly
from its internal FS (no per-frame USB streaming from the host).

Steps the script performs:
  1. connect to the screen (LcdCommTuringUSB)
  2. extract H.264 from MP4 and upload to /tmp/sdcard/mmcblk0p1/video/<name>.h264
  3. fire one of the three play opcodes (98 / 110 / 113) at the uploaded file
  4. sleep so you can watch the screen

Run from the repo root, inside the venv:

  python experiments/phase1_play_video.py rei/eva.rei/video/Finalrei.mp421103329.mp4

Useful options:
  --skip-upload    re-trigger playback without re-uploading
  --play-cmd N     try a different opcode (98 default, 110 alt, 113 image)
  --watch-seconds  how long to keep the script alive after firing play
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# logging must be set up BEFORE importing the library (which grabs its own logger)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# Make sure we can import the library when running from anywhere
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from library.lcd.lcd_comm_turing_usb import (  # noqa: E402
    LcdCommTuringUSB,
    upload_file,
    send_video,
    _play_command,
    _play2_command,
    _play3_command,
    _open_file_command,
    _resp_ok,
    send_brightness_command,
    send_refresh_storage_command,
)


PLAY_OPCODES = {98: _play_command, 110: _play2_command, 113: _play3_command}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("video", help="Path to MP4 file (local filesystem)")
    p.add_argument(
        "--mode",
        choices=["upload-play", "stream"],
        default="upload-play",
        help=(
            "upload-play (default): upload MP4 to device FS, then play from there. "
            "stream: skip upload, stream H.264 chunks live from host via cmd 121."
        ),
    )
    p.add_argument(
        "--loop-stream",
        action="store_true",
        help="When --mode stream, loop the H.264 forever (default: play once)",
    )
    p.add_argument(
        "--skip-upload",
        action="store_true",
        help="upload-play mode: skip upload, file already on device",
    )
    p.add_argument(
        "--play-cmd",
        type=int,
        default=98,
        choices=sorted(PLAY_OPCODES),
        help="Play opcode: 98=default, 110=alt, 113=image (default: 98)",
    )
    p.add_argument(
        "--brightness",
        type=int,
        default=60,
        help="Screen brightness 0-100 (default: 60)",
    )
    p.add_argument(
        "--watch-seconds",
        type=int,
        default=60,
        help="How long to keep the script running after firing play (default: 60s)",
    )
    p.add_argument(
        "--device-name",
        default=None,
        help=(
            "Override on-device file name. Default: <stem>.h264 derived from local."
            " Useful with --skip-upload to play a previously-uploaded file."
        ),
    )
    args = p.parse_args()

    video_path = Path(args.video).expanduser().resolve()
    if not video_path.exists() and not (args.mode == "upload-play" and args.skip_upload):
        print(f"ERROR: video not found: {video_path}", file=sys.stderr)
        return 1

    print(f"== Phase 1: video playback probe ==")
    print(f"Video : {video_path}")
    print(f"Mode  : {args.mode}")
    if args.mode == "upload-play":
        print(f"Skip upload : {args.skip_upload}")
        print(f"Play opcode : {args.play_cmd}")
    else:
        print(f"Loop stream : {args.loop_stream}")

    print(f"\n[step 1/4] Connecting to screen...")
    lcd = LcdCommTuringUSB()
    print(
        f"  detected PID=0x{lcd.dev_pid:04x}, native portrait size "
        f"{lcd.display_width}x{lcd.display_height}"
    )
    lcd.InitializeComm()
    send_brightness_command(lcd.dev, int(args.brightness / 100 * 102))

    print(f"\n[step 2] Inspecting on-device storage (before)...")
    try:
        send_refresh_storage_command(lcd.dev)
    except Exception as exc:
        print(f"  WARN: refresh_storage failed: {exc}")

    if args.mode == "stream":
        # Pure streaming path: host pushes H.264 chunks live via cmd 121.
        # No on-device storage required. This is the path the screen uses
        # internally in its native UsbMonitorL for ad-hoc playback.
        print(
            f"\n[step 3] Streaming H.264 from host (mode=stream, loop={args.loop_stream})...\n"
            f"  Ctrl+C to stop (sends CMD_STOP_STREAM)."
        )
        try:
            send_video(lcd.dev, str(video_path), loop=args.loop_stream)
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
        print("\nDone (stream mode).")
        return 0

    # --- upload-play mode ---
    if not args.skip_upload:
        print(f"\n[step 3] Uploading {video_path.name}...")
        # Probe the open-file response explicitly so we can see what the firmware actually returns
        # (upload_file() doesn't check _resp_ok on the open step — only on writes).
        from library.lcd.lcd_comm_turing_usb import extract_h264_from_mp4
        h264_path = extract_h264_from_mp4(str(video_path))
        device_name = (args.device_name or Path(h264_path).name)
        device_path_probe = f"/tmp/sdcard/mmcblk0p1/video/{device_name}"
        print(f"  Probing _open_file_command on {device_path_probe} ...")
        open_resp = _open_file_command(lcd.dev, device_path_probe)
        if open_resp:
            print(f"    response[:32] hex: {bytes(open_resp[:32]).hex()}")
            print(f"    _resp_ok: {_resp_ok(open_resp)}")
        else:
            print("    response: None")

        t0 = time.monotonic()
        ok = upload_file(lcd.dev, str(video_path))
        dt = time.monotonic() - t0
        if not ok:
            print("ERROR: upload_file returned False", file=sys.stderr)
            print(
                "\nDiagnosis hint: if 'Card Total = 0.00 MB' above, the screen has no\n"
                "writable storage mounted. Check if it needs a microSD card inserted,\n"
                "or try `--mode stream` (no storage required) to confirm playback works."
            )
            return 2
        print(f"  upload completed in {dt:.1f}s")
        try:
            send_refresh_storage_command(lcd.dev)
        except Exception as exc:
            print(f"  WARN: refresh_storage (post-upload) failed: {exc}")
    else:
        print(f"\n[step 3] Skipping upload (--skip-upload)")

    # Derive on-device path
    if args.device_name:
        device_name = args.device_name
    else:
        device_name = video_path.with_suffix(".h264").name
    device_path = f"/tmp/sdcard/mmcblk0p1/video/{device_name}"

    play_fn = PLAY_OPCODES[args.play_cmd]
    print(f"\n[step 4] Firing play opcode {args.play_cmd} on {device_path}...")
    resp = play_fn(lcd.dev, device_path)
    if resp:
        print(f"  response (first 32 bytes hex): {bytes(resp[:32]).hex()}")
        print(f"  _resp_ok: {_resp_ok(resp)}")
    else:
        print(f"  response: None")

    print(
        f"\nLook at the screen. Expecting Finalrei loop at native 25 fps.\n"
        f"Watching for {args.watch_seconds}s (Ctrl+C to exit sooner)..."
    )
    try:
        time.sleep(args.watch_seconds)
    except KeyboardInterrupt:
        print("\nInterrupted by user.")

    print(
        "\nDone. No stop command sent — screen should keep looping until you\n"
        "power-cycle it or run another command. Disconnect USB or relaunch\n"
        "main.py to take it back."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
