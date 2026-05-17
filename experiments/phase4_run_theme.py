"""Phase 4 CLI: run a YAML theme on the Turing 9.2" screen.

Usage from repo root, venv active:

    python experiments/phase4_run_theme.py res/themes/eva.rei/theme.yaml

Options:
  --duration N         run for N seconds, then stop cleanly (default: forever)
  --widget-period S    seconds between widget overlay updates (default: 1.0)
  --rotate-180         rotate widget overlay 180° to match physically flipped screen
  --screen 9.2|8.8     screen variant (default: 9.2)
  --dry-run            render one frame and save to /tmp/preview.png, don't connect

Stop with Ctrl+C; the engine stops the stream cleanly and exits.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from library.theme_engine import (  # noqa: E402
    ThemeEngine,
    load_theme,
    DataSourceRegistry,
    WidgetRenderer,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("theme_yaml", help="Path to theme.yaml")
    p.add_argument("--duration", type=float, default=None, help="Seconds to run (default: forever)")
    p.add_argument(
        "--widget-period",
        type=float,
        default=1.0,
        help="Seconds between widget overlays (default: 1.0)",
    )
    p.add_argument(
        "--rotate-180",
        action="store_true",
        help="Rotate widgets 180° (use if your screen is mounted flipped)",
    )
    p.add_argument(
        "--screen",
        choices=["8.8", "9.2"],
        default="9.2",
        help="Screen variant for native dimensions (default: 9.2)",
    )
    p.add_argument(
        "--dry-run",
        type=Path,
        nargs="?",
        const=Path("/tmp/theme_preview.png"),
        help="Render one frame, save to this path (default /tmp/theme_preview.png), don't open USB",
    )
    args = p.parse_args()

    theme = load_theme(args.theme_yaml)
    print(f"Loaded theme: {theme.name}  canvas={theme.canvas.width}x{theme.canvas.height}  widgets={len(theme.widgets)}")
    if theme.video:
        vp = theme.video_path
        ok = "ok" if vp and vp.exists() else "MISSING"
        print(f"  video: {theme.video.path}  ({ok})")
    else:
        print("  video: (none)")

    sources = DataSourceRegistry()

    if args.dry_run is not None:
        renderer = WidgetRenderer(theme, sources, screen=args.screen)
        # Prime psutil so first cpu_percent isn't 0
        import psutil
        psutil.cpu_percent(interval=None)
        time.sleep(0.5)
        img = renderer.render_frame(rotate_180=args.rotate_180)
        args.dry_run.parent.mkdir(parents=True, exist_ok=True)
        img.save(args.dry_run)
        print(f"\nDry-run: saved one frame to {args.dry_run}  ({img.width}x{img.height} RGBA)")
        return 0

    # Real run — connect to the screen
    from library.lcd.lcd_comm_turing_usb import LcdCommTuringUSB

    print("\nConnecting to screen...")
    lcd = LcdCommTuringUSB()
    print(f"  PID=0x{lcd.dev_pid:04x}  native_portrait={lcd.display_width}x{lcd.display_height}")
    lcd.InitializeComm()

    engine = ThemeEngine(theme, lcd, screen=args.screen, rotate_180=args.rotate_180)
    engine.run(duration=args.duration, widget_period=args.widget_period)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
