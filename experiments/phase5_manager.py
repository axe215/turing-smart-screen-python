"""Phase 5 entrypoint — run the multi-theme manager with web dashboard.

This launches:
  - a ThemeManager (catalog of res/themes/, swap-on-demand)
  - a Flask web UI on http://localhost:<port>/ (default 8765)

From the dashboard you can:
  - browse all themes in res/themes/
  - tune params (rotate 180°, rotate video, font scale, widget period)
  - activate any theme on the screen
  - stop the current theme
  - watch live status (uptime, widget/chunk counters)

Run from repo root, venv active:

  python experiments/phase5_manager.py

Open http://localhost:8765/ in your browser (or from your phone on
the same Wi-Fi: http://<host-ip>:8765/ once you've set --host 0.0.0.0).
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from library.theme_engine import ThemeManager  # noqa: E402
from library.theme_engine.web import create_app  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--themes-dir",
        type=Path,
        default=REPO_ROOT / "res" / "themes",
        help="Directory containing theme subfolders (default: res/themes/)",
    )
    p.add_argument("--host", default="127.0.0.1",
                   help="Flask bind host (default 127.0.0.1; use 0.0.0.0 for LAN access)")
    p.add_argument("--port", type=int, default=8765, help="Flask bind port (default 8765)")
    p.add_argument(
        "--autostart",
        type=str,
        default=None,
        metavar="DIR_NAME",
        help="Start with this theme already running (uses default params)",
    )
    args = p.parse_args()

    # Connect to the screen once; ThemeManager swaps engines on top of it.
    from library.lcd.lcd_comm_turing_usb import LcdCommTuringUSB  # noqa: E402

    print("Connecting to screen...")
    lcd = LcdCommTuringUSB()
    print(f"  PID=0x{lcd.dev_pid:04x}  native_portrait={lcd.display_width}x{lcd.display_height}")
    lcd.InitializeComm()

    manager = ThemeManager(themes_dir=args.themes_dir, lcd=lcd)
    available = manager.list_themes()
    print(f"Themes found ({len(available)}):")
    for t in available:
        print(f"  - {t.dir_name:30s} {t.canvas[0]}x{t.canvas[1]}  "
              f"widgets={t.widget_count}  video={t.has_video}")

    if args.autostart:
        try:
            manager.start(args.autostart)
            print(f"  autostart: {args.autostart}")
        except Exception as exc:
            print(f"  autostart failed: {exc}", file=sys.stderr)

    # Clean shutdown on Ctrl+C — stop the engine so the screen clears
    def _sigint_handler(sig, frame):
        print("\nShutting down — stopping theme engine...")
        manager.stop()
        sys.exit(0)
    signal.signal(signal.SIGINT, _sigint_handler)

    # Spin up Flask in the main thread (it owns the request loop)
    app = create_app(manager)
    print(f"\nDashboard: http://{args.host}:{args.port}/")
    # threaded=True so /api/status polling doesn't queue behind /api/start
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
