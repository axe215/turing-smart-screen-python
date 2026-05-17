"""Phase 3 CLI: parse a .turtheme file and print a human-readable report.

Usage from repo root:

    python experiments/phase3_parse_turtheme.py rei/eva.rei/theme/EVAREI.turtheme

What it prints:
  - Theme name, canvas size, colors, video paths
  - Every widget (Image / Data / Text / Chart) with X/Y, font, data binding
  - Optional --extract-bitmaps DIR to dump embedded preview/icon PNGs

This is purely a read-only inspection tool — no screen needed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from library.turtheme import (  # noqa: E402
    parse_turtheme,
    DataWidget,
    TextWidget,
    ChartWidget,
    ImageWidget,
    Color,
)


def fmt_color(c: Color) -> str:
    if c.is_default:
        return "(default)"
    return f"RGBA({c.r},{c.g},{c.b},{c.a})"


def print_report(theme, extract_dir: Path | None) -> int:
    print(f"=== {theme.name} ===")
    print(f"  Canvas        : {theme.width} x {theme.height}")
    print(f"  is_landscape  : {theme.is_landscape}")
    print(f"  front color   : {fmt_color(theme.front_color)}")
    print(f"  back color    : {fmt_color(theme.back_color)}")
    print(f"  visual theme  : {theme.is_visual_theme}")
    print(f"  temp theme    : {theme.is_temp_theme}")
    print(f"  aida theme    : {theme.is_aida_theme}")
    print(f"  video name    : {theme.video_name}")
    print(f"  video local   : {theme.video_path_local}")
    print(f"  video target  : {theme.video_target_path}")
    print(f"  widgets total : {len(theme.widgets)}")

    by_type: dict[str, list] = {}
    for w in theme.widgets:
        by_type.setdefault(w.type_name, []).append(w)

    for type_name in sorted(by_type):
        widgets = by_type[type_name]
        print(f"\n--- {type_name} ({len(widgets)}) ---")
        for w in widgets:
            extra = ""
            if isinstance(w, DataWidget):
                font_repr = (
                    f"{w.font.name} {w.font.size}px {fmt_color(w.font.color)}"
                    if w.font else "(no font)"
                )
                extra = (
                    f"  data='{w.data_name}'  sanma='{w.sanma_eng_name}'  "
                    f"show_unit={w.show_unit}  font={font_repr}"
                )
            elif isinstance(w, TextWidget):
                font_repr = (
                    f"{w.font.name} {w.font.size}px {fmt_color(w.font.color)}"
                    if w.font else "(no font)"
                )
                extra = f"  text={w.text!r}  font={font_repr}"
            elif isinstance(w, ChartWidget):
                extra = (
                    f"  {w.width}x{w.height}  max={w.max_value}  "
                    f"data='{w.data_name}'  line={fmt_color(w.line_color)}  "
                    f"fill={fmt_color(w.fill_color)}"
                )
            elif isinstance(w, ImageWidget):
                bm_bytes = len(w.bitmap_png) if w.bitmap_png else 0
                extra = (
                    f"  img='{w.image_name}'  zoom={w.zoom_rate:.3f}  "
                    f"bitmap_bytes={bm_bytes}"
                )

            flags = []
            if w.hide:
                flags.append("hide")
            if w.enabled:
                flags.append("enabled")
            flag_str = f" [{','.join(flags)}]" if flags else ""

            print(f"  ({w.x:5}, {w.y:5}) {w.display_name:<32}{flag_str}{extra}")

    if extract_dir:
        extract_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        for w in theme.widgets:
            if isinstance(w, ImageWidget) and w.bitmap_png:
                # Trust the source name but fall back to numeric
                stem = w.image_name or f"widget_{n}"
                # Strip extension if author included one in image_name
                stem = stem.rsplit(".", 1)[0]
                out = extract_dir / f"{stem}.png"
                # The bitmap blob may not actually be a PNG — could be the
                # original JPG. Sniff magic bytes.
                ext = ".png"
                if w.bitmap_png[:2] == b"\xff\xd8":
                    ext = ".jpg"
                elif w.bitmap_png[:4] == b"\x89PNG":
                    ext = ".png"
                else:
                    ext = ".bin"
                out = extract_dir / f"{stem}{ext}"
                out.write_bytes(w.bitmap_png)
                print(f"  wrote {out}  ({len(w.bitmap_png)} bytes)")
                n += 1
        print(f"\nExtracted {n} bitmaps to {extract_dir}")

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("turtheme", help="Path to a .turtheme file")
    p.add_argument(
        "--extract-bitmaps",
        type=Path,
        metavar="DIR",
        help="Dump embedded image widget bitmaps as PNG/JPG to this directory",
    )
    args = p.parse_args()

    in_path = Path(args.turtheme).expanduser().resolve()
    if not in_path.exists():
        print(f"ERROR: {in_path} not found", file=sys.stderr)
        return 1

    theme = parse_turtheme(in_path)
    return print_report(theme, args.extract_bitmaps)


if __name__ == "__main__":
    sys.exit(main())
