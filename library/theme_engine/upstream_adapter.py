"""Convert upstream mathoudebine themes to our axe215_v1 runtime dict.

This lets ThemeManager.start() launch a `Cyberpunk 2077`-style theme
without going through phase3_parse_turtheme. We read their YAML
straight from disk, walk the (very different) structure, and emit a
dict shaped like our YAML — which the existing runtime.build_runtime()
already knows how to turn into a ThemeRuntime.

Mapping cheat-sheet:

  display.DISPLAY_SIZE + DISPLAY_ORIENTATION  →  canvas (w, h)
  static_images.BACKGROUND.PATH               →  image.path
  static_text.<KEY>                           →  widget{type: text, text: TEXT}
  STATS.<METRIC>.<KIND>.TEXT                  →  widget{type: data, source: <mapped>}
  STATS.*.GRAPH / RADIAL / LINE_GRAPH         →  NOT YET — we skip + log

The result is opaque-image-mode (static background + widget overlay)
since upstream themes never carry video. The ThemeEngine's image path
(Phase 5h) handles those without spinning up a streaming thread.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


# Display sizes (landscape default). manager._UPSTREAM_DISPLAY_SIZE_MAP
# already has this table — we duplicate locally to keep modules independent.
_DISPLAY_SIZE = {
    "0.96": (160, 80),
    "2.1": (480, 480),
    "2.8": (480, 480),
    "3.5": (480, 320),
    "4.6": (960, 320),
    "5": (800, 480),
    "5.2": (1280, 720),
    "8": (1920, 480),
    "8.8": (1920, 480),
    "9.2": (1920, 462),
    "12.3": (1920, 720),
}


# STATS hierarchy → our `source:` string. Walked recursively.
# Only TEXT leaves map to actual widgets; other kinds (GRAPH, RADIAL,
# LINE_GRAPH, …) are noted in skip list.
_STATS_SOURCE_MAP = {
    # CPU
    ("CPU", "PERCENTAGE", "TEXT"): "cpu_percentage",
    ("CPU", "LOAD_ALL", "TEXT"): "cpu_percentage",
    ("CPU", "TEMPERATURE", "TEXT"): "cpu_temp",
    ("CPU", "FREQUENCY", "TEXT"): "cpu_freq",
    ("CPU", "FAN_SPEED", "TEXT"): "cpu_fan_speed",
    ("CPU", "POWER", "TEXT"): "cpu_power",
    # GPU
    ("GPU", "PERCENTAGE", "TEXT"): "gpu_percentage",
    ("GPU", "TEMPERATURE", "TEXT"): "gpu_temp",
    ("GPU", "FAN_SPEED", "TEXT"): "gpu_fan_speed",
    ("GPU", "POWER", "TEXT"): "gpu_power",
    ("GPU", "FPS", "TEXT"): "fps",
    # MEMORY
    ("MEMORY", "VIRTUAL", "PERCENT_TEXT"): "ram_percentage",
    ("MEMORY", "VIRTUAL", "USED_TEXT"): "ram_used_gb",
    ("MEMORY", "VIRTUAL", "TOTAL_TEXT"): "ram_total",
    # DATE / CLOCK
    ("DATE", "HOUR", "TEXT"): "clock_time",
    ("DATE", "DAY", "TEXT"): "clock_date",
    # NET
    ("NET", "WLO", "UPLOAD", "TEXT"): "net_upload",
    ("NET", "WLO", "DOWNLOAD", "TEXT"): "net_download",
    ("NET", "ETH", "UPLOAD", "TEXT"): "net_upload",
    ("NET", "ETH", "DOWNLOAD", "TEXT"): "net_download",
}


def _canvas_from_upstream(display: Dict[str, Any]) -> tuple:
    size_key = str(display.get("DISPLAY_SIZE", "")).strip().strip('"')
    w, h = _DISPLAY_SIZE.get(size_key, (1920, 480))
    orient = str(display.get("DISPLAY_ORIENTATION", "landscape")).lower()
    if orient.startswith("portrait"):
        w, h = h, w
    return (w, h)


def _color_list(spec: Any, default=(255, 255, 255, 255)) -> list:
    """Upstream stores FONT_COLOR as 'R, G, B' triple. Pad alpha to 255."""
    if isinstance(spec, str):
        try:
            parts = [int(p.strip()) for p in spec.split(",")]
        except ValueError:
            return list(default)
        if len(parts) == 3:
            return parts + [255]
        if len(parts) == 4:
            return parts
        return list(default)
    if isinstance(spec, (list, tuple)) and len(spec) in (3, 4):
        out = [int(c) for c in spec]
        return out + [255] if len(out) == 3 else out
    return list(default)


def _font_from_upstream(spec: Dict[str, Any], font_root: Optional[Path]) -> Dict[str, Any]:
    """Translate upstream FONT/FONT_SIZE/FONT_COLOR to our font dict.

    Upstream FONT is a relative path under res/fonts/, e.g.
    "roboto/Roboto-Bold.ttf". We pass the *absolute* path as the
    `family` field — Pillow's ImageFont.truetype accepts a path there,
    and our renderer just hands it through.
    """
    out: Dict[str, Any] = {}
    font_rel = spec.get("FONT")
    if font_rel:
        if font_root is not None:
            abs_path = (font_root / str(font_rel)).resolve()
            out["family"] = str(abs_path)
        else:
            out["family"] = str(font_rel)
    if "FONT_SIZE" in spec:
        try:
            out["size"] = int(spec["FONT_SIZE"])
        except (TypeError, ValueError):
            pass
    color = _color_list(spec.get("FONT_COLOR"))
    out["color"] = color
    return out


def _maybe_widget_from_stats_leaf(path: tuple, leaf: Dict[str, Any], font_root) -> Optional[Dict[str, Any]]:
    """Given the hierarchy path and the leaf dict (last node like
    {SHOW, X, Y, FONT, FONT_SIZE, …}), return a widget dict or None."""
    if not isinstance(leaf, dict):
        return None
    show = leaf.get("SHOW")
    # Honor SHOW=false (hide widget) — emit but with hide=true so user
    # can re-enable in our YAML if they want.
    hide = show is False
    source = _STATS_SOURCE_MAP.get(path)
    if source is None:
        # Unknown leaf — don't emit a widget. Log once per path so we
        # can extend the map if needed.
        log.debug("stats path %s has no source mapping", " / ".join(path))
        return None
    x = leaf.get("X")
    y = leaf.get("Y")
    if x is None or y is None:
        return None
    widget = {
        "id": "_".join(p.lower() for p in path),
        "type": "data",
        "source": source,
        "x": int(x),
        "y": int(y),
        "show_unit": bool(leaf.get("SHOW_UNIT", False)),
        "font": _font_from_upstream(leaf, font_root),
    }
    if hide:
        widget["hide"] = True
    return widget


def _walk_stats(stats: Any, path: tuple, font_root) -> List[Dict[str, Any]]:
    """Recursively walk the STATS hierarchy collecting TEXT widget dicts."""
    out: List[Dict[str, Any]] = []
    if not isinstance(stats, dict):
        return out
    for k, v in stats.items():
        sub_path = path + (str(k).upper(),)
        if isinstance(v, dict):
            # A "leaf" is one whose path matches our source map or has
            # X+Y. We try both: lookup with current path; recurse to find deeper TEXT.
            if sub_path in _STATS_SOURCE_MAP:
                w = _maybe_widget_from_stats_leaf(sub_path, v, font_root)
                if w is not None:
                    out.append(w)
            else:
                out.extend(_walk_stats(v, sub_path, font_root))
    return out


def upstream_to_axe215_dict(
    upstream_data: Dict[str, Any],
    theme_dir: Path,
    font_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build a dict that runtime.build_runtime() can consume.

    `theme_dir` is the upstream theme's folder (where background.png
    lives). `font_root` is the directory containing the upstream font
    tree (typically <repo>/res/fonts/). If font_root is None we try
    theme_dir/../../fonts as a default.
    """
    theme_dir = Path(theme_dir)
    if font_root is None:
        # res/themes/<theme>/  → res/fonts/
        candidate = theme_dir.parent.parent / "fonts"
        if candidate.exists():
            font_root = candidate

    display = upstream_data.get("display") or {}
    static_text = upstream_data.get("static_text") or {}
    static_images = upstream_data.get("static_images") or {}
    stats = upstream_data.get("STATS") or {}

    # Canvas
    canvas_w, canvas_h = _canvas_from_upstream(display)
    background_widget = static_images.get("BACKGROUND") if isinstance(static_images, dict) else None
    if isinstance(background_widget, dict):
        try:
            bw = int(background_widget.get("WIDTH", 0))
            bh = int(background_widget.get("HEIGHT", 0))
            if bw and bh:
                canvas_w, canvas_h = bw, bh
        except (TypeError, ValueError):
            pass

    # Static background image (always present in upstream themes)
    image_block = None
    if isinstance(background_widget, dict):
        rel = background_widget.get("PATH")
        if rel:
            # Upstream BACKGROUND PATH is relative to the theme folder.
            image_block = {"path": str(rel)}

    # Widgets: static_text + STATS leaves
    widgets: List[Dict[str, Any]] = []
    if isinstance(static_text, dict):
        for key, spec in static_text.items():
            if not isinstance(spec, dict):
                continue
            x = spec.get("X")
            y = spec.get("Y")
            if x is None or y is None:
                continue
            widgets.append({
                "id": f"text_{str(key).lower()}",
                "type": "text",
                "text": str(spec.get("TEXT", key)),
                "x": int(x),
                "y": int(y),
                "font": _font_from_upstream(spec, font_root),
            })
    widgets.extend(_walk_stats(stats, tuple(), font_root))

    out = {
        "schema_version": 1,
        "name": str(upstream_data.get("author") or theme_dir.name),
        "source": "upstream_adapter",
        "canvas": {"width": canvas_w, "height": canvas_h},
        "widgets": widgets,
    }
    if image_block:
        out["image"] = image_block
    return out
