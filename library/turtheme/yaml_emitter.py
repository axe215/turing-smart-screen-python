"""Convert a ThemeDef into our YAML-based theme format + dump asset files.

Output layout for a theme named "eva.rei":

  <out_root>/eva.rei/
      theme.yaml              # our extended format (see below)
      video/<name>.mp4        # copy of the source video, if available
      images/<name>.png       # bitmaps extracted from the .turtheme

YAML schema (v1):

  schema_version: 1
  name: EVAREI
  source: turtheme            # turtheme | handwritten | mathoudebine
  canvas:
    width: 1920
    height: 480
  video:
    path: video/Finalrei.mp4
    loop: true
    framerate: 25
  widgets:
    - id: cpu_temp_value
      type: data              # data | text | chart | image
      source: cpu_temp        # normalized data id used by the engine
      legacy_source: CPUTEMP  # original UsbMonitorL DataName, for traceability
      x: 643
      y: 112
      show_unit: false
      font: {family: "Digital Dismay", size: 74, bold: false, color: [255,255,255,255]}
    - id: ...
      ...

Widget z-order = list order (first item drawn first; later items on top).
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .parser import (
    ThemeDef,
    WidgetDef,
    DataWidget,
    TextWidget,
    ChartWidget,
    ImageWidget,
    Color,
    FontConfig,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source-name normalization
# ---------------------------------------------------------------------------

# Map UsbMonitorL's `DataName` strings to our canonical engine source ids.
# The engine binds these to real sensor readings in Phase 4.
LEGACY_SOURCE_MAP = {
    "CPUTEMP": "cpu_temp",
    "CPULOAD": "cpu_percentage",
    "CPUFAN": "cpu_fan_speed",
    "CPUPWR": "cpu_power",
    "CPUMODEL": "cpu_model",
    "GPUTEMP": "gpu_temp",
    "GPULOAD": "gpu_percentage",
    "GPUFAN": "gpu_fan_speed",
    "GPUPWR": "gpu_power",
    "GPUMODEL": "gpu_model",
    "RAMTOTAL": "ram_total",
    "RAM_GB": "ram_used_gb",
    "RAMLOAD": "ram_percentage",
    "TIME": "clock_time",
    "DATE": "clock_date",
    "FPS": "fps",  # no upstream binding; user provides via RTSS or stays static
    "UPSPEED": "net_upload",
    "DOWNDSPEED": "net_download",  # original typo in UsbMonitorL
    "DOWNSPEED": "net_download",
}


def normalize_source(legacy: str) -> str:
    if not legacy:
        return ""
    return LEGACY_SOURCE_MAP.get(legacy.upper(), legacy.lower().replace(" ", "_"))


def slug_id(s: str, fallback: str) -> str:
    """Make a unique-ish slug from a DisplayName like 'Data--Cpu Temp'."""
    if not s:
        return fallback
    base = s.replace("--", "_").replace(" ", "_").replace("/", "_").replace("\\", "_")
    base = "".join(c for c in base if c.isalnum() or c == "_")
    base = base.strip("_").lower()
    return base or fallback


# ---------------------------------------------------------------------------
# Color / font dict converters
# ---------------------------------------------------------------------------


def _color_to_list(c: Color) -> Optional[List[int]]:
    if c is None or c.is_default:
        return None
    return [int(c.r), int(c.g), int(c.b), int(c.a)]


def _font_to_dict(fc: Optional[FontConfig]) -> Optional[Dict[str, Any]]:
    if fc is None or not fc.name:
        return None
    out: Dict[str, Any] = {
        "family": fc.name,
        "size": int(fc.size),
    }
    if fc.is_bold:
        out["bold"] = True
    color_list = _color_to_list(fc.color)
    if color_list is not None:
        out["color"] = color_list
    if fc.alignment.horizontal != 0 or fc.alignment.vertical != 0:
        out["alignment"] = [fc.alignment.horizontal, fc.alignment.vertical]
    return out


def _widget_to_dict(
    w: WidgetDef,
    idx: int,
    used_ids: set,
    image_rename_map: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    # Generate a unique id by adding a numeric suffix on collision
    base_id = slug_id(w.display_name, f"widget_{idx}")
    wid = base_id
    n = 2
    while wid in used_ids:
        wid = f"{base_id}_{n}"
        n += 1
    used_ids.add(wid)

    base: Dict[str, Any] = {
        "id": wid,
        "x": int(w.x),
        "y": int(w.y),
    }
    if w.hide:
        base["hide"] = True
    # `enabled` in the saved .turtheme is always False (.NET default) and
    # doesn't reflect runtime visibility — emit only when explicitly True
    # so we don't pollute the YAML with noise.
    if w.enabled:
        base["enabled"] = True

    if isinstance(w, DataWidget):
        base["type"] = "data"
        if w.data_name:
            base["legacy_source"] = w.data_name
            base["source"] = normalize_source(w.data_name)
        else:
            base["source"] = ""
        if w.sanma_eng_name and w.sanma_eng_name != w.data_name:
            base["legacy_sanma"] = w.sanma_eng_name
        if w.show_unit:
            base["show_unit"] = True
        font = _font_to_dict(w.font)
        if font:
            base["font"] = font
        return base

    if isinstance(w, TextWidget):
        base["type"] = "text"
        base["text"] = w.text
        font = _font_to_dict(w.font)
        if font:
            base["font"] = font
        return base

    if isinstance(w, ChartWidget):
        base["type"] = "chart"
        base["width"] = int(w.width)
        base["height"] = int(w.height)
        if w.data_name:
            base["legacy_source"] = w.data_name
            base["source"] = normalize_source(w.data_name)
        base["max_value"] = float(w.max_value)
        line = _color_to_list(w.line_color)
        if line is not None:
            base["line_color"] = line
        fill = _color_to_list(w.fill_color)
        if fill is not None:
            base["fill_color"] = fill
        border = _color_to_list(w.border_color)
        if border is not None:
            base["border_color"] = border
        if w.line_width != 1:
            base["line_width"] = int(w.line_width)
        if w.border_width != 1:
            base["border_width"] = int(w.border_width)
        if w.column_width != 5:
            base["column_width"] = int(w.column_width)
        if w.coefficient != 1.0:
            base["coefficient"] = float(w.coefficient)
        return base

    if isinstance(w, ImageWidget):
        base["type"] = "image"
        if w.image_name:
            # Use whatever filename export_theme_dir actually wrote
            # (extension may differ from the original .turtheme image_name
            # because we sniff magic bytes — e.g. ".jpg" labelled bytes
            # may actually be PNG).
            if image_rename_map and w.image_name in image_rename_map:
                base["image"] = f"images/{image_rename_map[w.image_name]}"
            else:
                base["image"] = f"images/{w.image_name}"
        if abs(w.zoom_rate - 1.0) > 1e-6:
            base["scale"] = round(float(w.zoom_rate), 4)
        # NOTE on big image widgets: a large bitmap (scale ~0.64) usually
        # turns out to be UsbMonitorL's *runtime overlay graphic* — the
        # tech-circuit decorations, labels, and icons that sit between the
        # video and the live data widgets. Don't auto-hide it. Users who
        # want to suppress can add `hide: true` by hand.
        return base

    return None


def to_yaml_dict(
    theme: ThemeDef,
    video_filename: Optional[str],
    image_rename_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Build the YAML-ready dict from a ThemeDef.

    `video_filename` is the relative path to write into the YAML (e.g.
    "video/Finalrei.mp421103329.mp4"). Pass None if no video.

    `image_rename_map` maps original `image_name` (from .turtheme) to the
    actual filename written by export_theme_dir (so the YAML points at
    the file that exists on disk).
    """
    out: Dict[str, Any] = {
        "schema_version": 1,
        "name": theme.name,
        "source": "turtheme",
        "canvas": {"width": int(theme.width), "height": int(theme.height)},
    }
    if video_filename:
        out["video"] = {
            "path": video_filename,
            "loop": True,
            "framerate": 25,
        }
    front = _color_to_list(theme.front_color)
    back = _color_to_list(theme.back_color)
    if front or back:
        colors: Dict[str, Any] = {}
        if front:
            colors["front"] = front
        if back:
            colors["back"] = back
        out["theme_colors"] = colors

    widgets_out: List[Dict[str, Any]] = []
    used_ids: set = set()
    for i, w in enumerate(theme.widgets):
        d = _widget_to_dict(w, i, used_ids, image_rename_map=image_rename_map)
        if d is not None:
            widgets_out.append(d)
    out["widgets"] = widgets_out
    return out


# ---------------------------------------------------------------------------
# Theme directory export
# ---------------------------------------------------------------------------


def _safe_filename(name: str, default: str, ext: str) -> str:
    name = name or default
    # Drop dangerous path components, keep extension
    name = Path(name).name
    if "." not in name:
        name = name + ext
    return name


def export_theme_dir(
    theme: ThemeDef,
    out_dir: Path,
    video_src: Optional[Path] = None,
    fonts_src: Optional[Path] = None,
    write_bitmaps: bool = True,
) -> Path:
    """Write a complete theme directory: theme.yaml + video/ + images/ + fonts/.

    `video_src` is an optional path to the source MP4 — if given, it's
    copied into <out_dir>/video/. If omitted, no video file is written
    but the YAML still references one (using the videoName from the
    .turtheme), which the user can drop in later.

    `fonts_src` is an optional path to a fonts directory — its .ttf/.otf
    files will be copied into <out_dir>/fonts/ so the renderer can find
    them by family name.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- video ----
    video_filename: Optional[str] = None
    if theme.video_name:
        video_filename = f"video/{theme.video_name}"
        if video_src is not None:
            vsrc = Path(video_src)
            if vsrc.exists():
                vdir = out_dir / "video"
                vdir.mkdir(exist_ok=True)
                vdst = vdir / theme.video_name
                if not vdst.exists():
                    shutil.copy2(vsrc, vdst)
                log.info("copied video: %s → %s", vsrc, vdst)
            else:
                log.warning("video_src %s does not exist; skipped copy", vsrc)

    # ---- fonts ----
    if fonts_src is not None:
        fsrc = Path(fonts_src)
        if fsrc.exists() and fsrc.is_dir():
            fdst_dir = out_dir / "fonts"
            fdst_dir.mkdir(exist_ok=True)
            copied = 0
            for f in fsrc.iterdir():
                if f.is_file() and f.suffix.lower() in (".ttf", ".otf", ".ttc"):
                    dst = fdst_dir / f.name
                    if not dst.exists():
                        shutil.copy2(f, dst)
                        copied += 1
            log.info("copied %d font files into %s", copied, fdst_dir)
        else:
            log.warning("fonts_src %s not a directory; skipped fonts copy", fsrc)

    # ---- bitmaps ----
    image_rename_map: Dict[str, str] = {}
    if write_bitmaps:
        img_dir = out_dir / "images"
        for i, w in enumerate(theme.widgets):
            if isinstance(w, ImageWidget) and w.bitmap_png:
                original_name = w.image_name or f"image_{i}.png"
                stem = Path(_safe_filename(original_name, f"image_{i}", ".png")).stem
                # Sniff magic for actual ext
                bm = w.bitmap_png
                if bm[:2] == b"\xff\xd8":
                    ext = ".jpg"
                elif bm[:4] == b"\x89PNG":
                    ext = ".png"
                else:
                    ext = ".bin"
                actual_name = f"{stem}{ext}"
                img_dir.mkdir(exist_ok=True)
                out_path = img_dir / actual_name
                if not out_path.exists():
                    out_path.write_bytes(bm)
                    log.info("wrote bitmap: %s (%d bytes)", out_path, len(bm))
                # Remember the rename so YAML points at the real filename
                image_rename_map[original_name] = actual_name

    # ---- yaml ----
    data = to_yaml_dict(theme, video_filename=video_filename, image_rename_map=image_rename_map)
    yaml_path = out_dir / "theme.yaml"
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=120)
    log.info("wrote yaml: %s", yaml_path)
    return out_dir
