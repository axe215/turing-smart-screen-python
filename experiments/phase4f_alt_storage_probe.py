"""Probe alternative on-device storage path: /mnt/SDCARD/

Background: In Phase 1, upload_file() to /tmp/sdcard/mmcblk0p1/video/
failed on this 9.2" (write timed out at chunk 1, Card Total reported as
0.00 MB). We concluded the FS-based playback path doesn't work because
the screen has no microSD slot.

BUT: Phase 3 parsing of EVAREI.turtheme found a different on-device path
the official UsbMonitorL software uses:

    videoTargetPath = /mnt/SDCARD/video/Finalrei.mp421103329.mp4

Note "/mnt/SDCARD/" vs upstream's "/tmp/sdcard/mmcblk0p1/". Even though
the 9.2" has no physical SD slot, the firmware may have /mnt/SDCARD/
mounted to internal flash. UsbMonitorL apparently uses it successfully.

This probe:

  1. Tries open_file/write_file/play with /mnt/SDCARD/video/<name>.h264
  2. Also tries open_file with a small synthetic file to see if open
     responds positively (compared to /tmp/sdcard/... which errored)
  3. Cycles through three play opcodes (98 / 110 / 113) if upload succeeds

If THIS path works on the 9.2", we can ditch live streaming entirely:
upload the H.264 once, fire play, screen loops it autonomously, and
the host only sends widget overlays — no chunked H.264 traffic at all.
This would also eliminate the per-overlay micro-freezes since there's
no streaming pipeline to disrupt.

Usage:

    # Full test: upload + try to play
    python experiments/phase4f_alt_storage_probe.py rei/eva.rei/video/Finalrei.mp421103329.mp4

    # Just try opening a path (no actual write):
    python experiments/phase4f_alt_storage_probe.py --probe-paths

    # Skip upload, play an already-uploaded file:
    python experiments/phase4f_alt_storage_probe.py rei/eva.rei/video/Finalrei.mp421103329.mp4 --skip-upload
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from library.lcd.lcd_comm_turing_usb import (  # noqa: E402
    LcdCommTuringUSB,
    extract_h264_from_mp4,
    build_command_packet_header,
    encrypt_command_packet,
    write_to_device,
    _open_file_command,
    _write_file_command,
    _play_command,
    _play2_command,
    _play3_command,
    _resp_ok,
    send_brightness_command,
    send_refresh_storage_command,
)


PLAY_OPCODES = {98: _play_command, 110: _play2_command, 113: _play3_command}


def probe_open_paths(lcd):
    """Just probe several candidate paths with _open_file_command and report
    the responses. Doesn't actually write any data."""
    candidates = [
        "/mnt/SDCARD/video/probe.h264",
        "/mnt/SDCARD/probe.h264",
        "/mnt/sdcard/video/probe.h264",  # lowercase variant
        "/sdcard/video/probe.h264",
        "/tmp/sdcard/mmcblk0p1/video/probe.h264",  # upstream baseline
        "/storage/video/probe.h264",
        "/data/video/probe.h264",
        "/var/video/probe.h264",
    ]
    print("\n=== Probing candidate paths via _open_file_command ===")
    for path in candidates:
        resp = _open_file_command(lcd.dev, path)
        if resp is None:
            tag = "TIMEOUT/NONE"
        else:
            tag = "OK" if _resp_ok(resp) else "REJECTED"
        head = bytes(resp[:24]).hex() if resp else "—"
        print(f"  {path:55s} → {tag}  resp[:24]={head}")
        time.sleep(0.2)


def upload_to_path(lcd, local_h264: Path, device_path: str) -> bool:
    print(f"\n=== Uploading {local_h264.name} → {device_path} ===")
    print(f"  _open_file_command({device_path}) ...")
    resp = _open_file_command(lcd.dev, device_path)
    if resp is None:
        print("    NO RESPONSE")
        return False
    print(f"    resp[:24]={bytes(resp[:24]).hex()}  _resp_ok={_resp_ok(resp)}")

    print(f"  _write_file_command(local={local_h264}) ...")
    t0 = time.monotonic()
    ok = _write_file_command(lcd.dev, str(local_h264))
    dt = time.monotonic() - t0
    print(f"    returned {ok} in {dt:.1f}s")
    return ok


def try_play(lcd, device_path: str, opcode: int, watch_seconds: int):
    fn = PLAY_OPCODES[opcode]
    print(f"\n=== Trying play opcode {opcode} on {device_path} ===")
    resp = fn(lcd.dev, device_path)
    if resp is None:
        print("  NO RESPONSE")
        return False
    head = bytes(resp[:24]).hex()
    ok = _resp_ok(resp)
    print(f"  resp[:24]={head}  _resp_ok={ok}")
    print(f"  Watching for {watch_seconds}s. Look at the screen.")
    time.sleep(watch_seconds)
    return ok


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("video", nargs="?", default=None, help="MP4 to upload (omit with --probe-paths)")
    p.add_argument("--device-dir", default="/mnt/SDCARD/video", help="Remote dir to upload to")
    p.add_argument(
        "--probe-paths",
        action="store_true",
        help="Probe candidate open paths with _open_file_command, don't write data",
    )
    p.add_argument("--skip-upload", action="store_true", help="Skip upload; play existing file")
    p.add_argument(
        "--play-opcodes",
        default="98,110,113",
        help="Comma-separated play opcodes to try in order (default: 98,110,113)",
    )
    p.add_argument("--watch-seconds", type=int, default=10, help="Seconds to watch per play attempt")
    args = p.parse_args()

    print("Connecting to screen...")
    lcd = LcdCommTuringUSB()
    print(f"  PID=0x{lcd.dev_pid:04x}, native portrait {lcd.display_width}x{lcd.display_height}")
    lcd.InitializeComm()
    send_brightness_command(lcd.dev, 51)  # bright enough to see

    print("\n=== Storage report (refresh_storage cmd 100) ===")
    try:
        send_refresh_storage_command(lcd.dev)
    except Exception as exc:
        print(f"  refresh_storage failed: {exc}")

    if args.probe_paths:
        probe_open_paths(lcd)
        return 0

    if not args.video:
        print("ERROR: provide a video path or use --probe-paths", file=sys.stderr)
        return 1

    video_path = Path(args.video).expanduser().resolve()
    if not video_path.exists() and not args.skip_upload:
        print(f"ERROR: video {video_path} not found", file=sys.stderr)
        return 2

    # Extract H.264 once on host (the screen wants Annex-B)
    h264 = Path(extract_h264_from_mp4(str(video_path)))
    device_path = f"{args.device_dir.rstrip('/')}/{h264.name}"

    if not args.skip_upload:
        ok = upload_to_path(lcd, h264, device_path)
        if not ok:
            print("\nUpload failed. /mnt/SDCARD/ probably isn't writable either.")
            print("Re-run with --probe-paths to scan for a usable path.")
            return 3
        try:
            send_refresh_storage_command(lcd.dev)
        except Exception:
            pass

    for op_str in args.play_opcodes.split(","):
        op = int(op_str.strip())
        if op not in PLAY_OPCODES:
            continue
        ok = try_play(lcd, device_path, op, args.watch_seconds)
        if ok:
            print(f"\n✅ Opcode {op} accepted! Look at screen — is video playing?")
        else:
            print(f"\n⚠️ Opcode {op} response wasn't OK — continuing")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
