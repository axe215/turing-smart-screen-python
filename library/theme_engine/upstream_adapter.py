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
  STATS.*.GRAPH                               →  widget{type: progress_bar}
  STATS.*.RADIAL                              →  widget{type: radial}
  STATS.*.LINE_GRAPH                          →  widget{type: line_graph}

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


# Widget kinds the upstream STATS hierarchy uses as leaf keys.
_WIDGET_KINDS = {
    "TEXT",
    "PERCENT_TEXT", "USED_TEXT", "FREE_TEXT", "TOTAL_TEXT",
    "GRAPH",
    "RADIAL",
    "LINE_GRAPH",
}

# Parent-path → source. The metric stays the same regardless of how
# it's visualized (TEXT vs GRAPH vs RADIAL vs LINE_GRAPH), so we
# resolve by the PATH UP TO BUT NOT INCLUDING the widget-kind leaf.
_STATS_METRIC_SOURCE = {
    # CPU
    ("CPU", "PERCENTAGE"): "cpu_percentage",
    ("CPU", "LOAD_ALL"): "cpu_percentage",
    ("CPU", "TEMPERATURE"): "cpu_temp",
    ("CPU", "FREQUENCY"): "cpu_freq",
    ("CPU", "FAN_SPEED"): "cpu_fan_speed",
    ("CPU", "POWER"): "cpu_power",
    # GPU
    ("GPU", "PERCENTAGE"): "gpu_percentage",
    ("GPU", "TEMPERATURE"): "gpu_temp",
    ("GPU", "FAN_SPEED"): "gpu_fan_speed",
    ("GPU", "POWER"): "gpu_power",
    ("GPU", "FPS"): "fps",
    # MEMORY.VIRTUAL.GRAPH / .RADIAL bind to percentage; the per-leaf
    # TEXT variants (PERCENT/USED/FREE/TOTAL_TEXT) override below.
    ("MEMORY", "VIRTUAL"): "ram_percentage",
    # DATE / NET (leaves are TEXT-only; mapped below for completeness)
    ("DATE", "HOUR"): "clock_time",
    ("DATE", "DAY"): "clock_date",
    ("NET", "WLO", "UPLOAD"): "net_upload",
    ("NET", "WLO", "DOWNLOAD"): "net_download",
    ("NET", "ETH", "UPLOAD"): "net_upload",
    ("NET", "ETH", "DOWNLOAD"): "net_download",
}

# Full-path (including leaf name) → source. Used for memory leaves
# where the SAME parent path emits FOUR different metrics depending
# on which TEXT-variant is chosen.
_STATS_LEAF_SOURCE = {
    ("MEMORY", "VIRTUAL", "PERCENT_TEXT"): "ram_percentage",
    ("MEMORY", "VIRTUAL", "USED_TEXT"): "ram_used_gb",
    ("MEMORY", "VIRTUAL", "TOTAL_TEXT"): "ram_total",
}


def _resolve_source(path: tuple) -> Optional[str]:
    """Look up the source for a STATS path. Tries full-path first, then
    falls back to parent-path (path-without-leaf)."""
    if path in _STATS_LEAF_SOURCE:
        return _STATS_LEAF_SOURCE[path]
    return _STATS_METRIC_SOURCE.get(path[:-1])


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


def _axis_font_from_upstream(spec: Dict[str, Any], font_root: Optional[Path]) -> Dict[str, Any]:
    """Variant of _font_from_upstream that reads AXIS_FONT/AXIS_FONT_SIZE.

    Used by LINE_GRAPH for the axis tick labels — upstream stores those
    under a different prefix than the main FONT/FONT_SIZE fields.
    """
    out: Dict[str, Any] = {}
    font_rel = spec.get("AXIS_FONT")
    if font_rel:
        if font_root is not None:
            out["family"] = str((font_root / str(font_rel)).resolve())
        else:
            out["family"] = str(font_rel)
    if "AXIS_FONT_SIZE" in spec:
        try:
            out["size"] = int(spec["AXIS_FONT_SIZE"])
        except (TypeError, ValueError):
            pass
    out["color"] = _color_list(spec.get("AXIS_COLOR"), default=(255, 255, 255, 255))
    return out


def _id_from_path(path: tuple) -> str:
    return "_".join(p.lower() for p in path)


def _build_text_widget(path: tuple, leaf: Dict[str, Any], source: str, font_root) -> Optional[Dict[str, Any]]:
    """Build a data widget (numeric text from a source)."""
    x = leaf.get("X")
    y = leaf.get("Y")
    if x is None or y is None:
        return None
    out = {
        "id": _id_from_path(path),
        "type": "data",
        "source": source,
        "x": int(x),
        "y": int(y),
        "show_unit": bool(leaf.get("SHOW_UNIT", False)),
        "font": _font_from_upstream(leaf, font_root),
    }
    # Honor explicit MIN_SIZE override (upstream stats.py:101). Absent
    # → renderer falls back to DEFAULT_MIN_SIZE per source.
    if "MIN_SIZE" in leaf:
        try:
            out["min_size"] = int(leaf["MIN_SIZE"])
        except (TypeError, ValueError):
            pass
    return out


def _build_progress_bar(path: tuple, leaf: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
    """Map upstream GRAPH (horizontal/vertical progress bar) → progress_bar widget."""
    x = leaf.get("X")
    y = leaf.get("Y")
    w = leaf.get("WIDTH")
    h = leaf.get("HEIGHT")
    if any(v is None for v in (x, y, w, h)):
        return None
    out = {
        "id": _id_from_path(path),
        "type": "progress_bar",
        "source": source,
        "x": int(x),
        "y": int(y),
        "width": int(w),
        "height": int(h),
        "min_value": float(leaf.get("MIN_VALUE", 0)),
        "max_value": float(leaf.get("MAX_VALUE", 100)),
        "bar_color": _color_list(leaf.get("BAR_COLOR"), default=(0, 255, 0, 255)),
        "bar_outline": bool(leaf.get("BAR_OUTLINE", False)),
        "reverse_direction": bool(leaf.get("REVERSE_DIRECTION", False)),
    }
    if "BACKGROUND_COLOR" in leaf:
        out["background_color"] = _color_list(leaf.get("BACKGROUND_COLOR"), default=(0, 0, 0, 255))
    return out


def _build_radial(path: tuple, leaf: Dict[str, Any], source: str, font_root) -> Optional[Dict[str, Any]]:
    """Map upstream RADIAL (dial / radial progress) → radial widget."""
    x = leaf.get("X")
    y = leaf.get("Y")
    radius = leaf.get("RADIUS")
    if any(v is None for v in (x, y, radius)):
        return None
    out = {
        "id": _id_from_path(path),
        "type": "radial",
        "source": source,
        "x": int(x),
        "y": int(y),
        "radius": int(radius),
        "width": int(leaf.get("WIDTH", 10)),
        "min_value": float(leaf.get("MIN_VALUE", 0)),
        "max_value": float(leaf.get("MAX_VALUE", 100)),
        "angle_start": float(leaf.get("ANGLE_START", 0)),
        "angle_end": float(leaf.get("ANGLE_END", 360)),
        "angle_steps": int(leaf.get("ANGLE_STEPS", 0) or 0),
        "angle_sep": float(leaf.get("ANGLE_SEP", 0) or 0),
        "clockwise": bool(leaf.get("CLOCKWISE", True)),
        "bar_color": _color_list(leaf.get("BAR_COLOR"), default=(0, 255, 0, 255)),
        "show_text": bool(leaf.get("SHOW_TEXT", False)),
        "show_unit": bool(leaf.get("SHOW_UNIT", False)),
        "font": _font_from_upstream(leaf, font_root),
    }
    if "BACKGROUND_COLOR" in leaf:
        out["background_color"] = _color_list(leaf.get("BACKGROUND_COLOR"), default=(0, 0, 0, 255))
    return out


def _build_line_graph(path: tuple, leaf: Dict[str, Any], source: str, font_root) -> Optional[Dict[str, Any]]:
    """Map upstream LINE_GRAPH → line_graph widget."""
    x = leaf.get("X")
    y = leaf.get("Y")
    w = leaf.get("WIDTH")
    h = leaf.get("HEIGHT")
    if any(v is None for v in (x, y, w, h)):
        return None
    out = {
        "id": _id_from_path(path),
        "type": "line_graph",
        "source": source,
        "x": int(x),
        "y": int(y),
        "width": int(w),
        "height": int(h),
        "min_value": float(leaf.get("MIN_VALUE", 0)),
        "max_value": float(leaf.get("MAX_VALUE", 100)),
        "history_size": int(leaf.get("HISTORY_SIZE", 60)),
        "autoscale": bool(leaf.get("AUTOSCALE", False)),
        "line_color": _color_list(leaf.get("LINE_COLOR"), default=(255, 255, 255, 255)),
        "line_width": int(leaf.get("LINE_WIDTH", 1)),
        "axis": bool(leaf.get("AXIS", False)),
        "axis_color": _color_list(leaf.get("AXIS_COLOR"), default=(255, 255, 255, 255)),
        "axis_font": _axis_font_from_upstream(leaf, font_root),
    }
    if "BACKGROUND_COLOR" in leaf:
        out["background_color"] = _color_list(leaf.get("BACKGROUND_COLOR"), default=(0, 0, 0, 255))
    return out


# Leaf-kind dispatch. Each builder returns either a widget dict or None.
_KIND_DISPATCH = {
    "TEXT":         "text",
    "PERCENT_TEXT": "text",
    "USED_TEXT":    "text",
    "FREE_TEXT":    "text",
    "TOTAL_TEXT":   "text",
    "GRAPH":        "progress_bar",
    "RADIAL":       "radial",
    "LINE_GRAPH":   "line_graph",
}


def _build_widget_from_leaf(path: tuple, leaf: Dict[str, Any], font_root) -> Optional[Dict[str, Any]]:
    """Given the hierarchy path (ending in a _WIDGET_KINDS name) and the
    leaf dict, build a widget dict of the appropriate type or return None
    if the leaf can't be mapped or has SHOW: False.

    SHOW: False is honored at the *leaf* level — we just skip the widget
    entirely (vs. emitting hide=true) since upstream's intent is "do not
    render this," same as our enabled=False.
    """
    if not isinstance(leaf, dict):
        return None
    if leaf.get("SHOW") is False:
        return None

    kind = path[-1]
    source = _resolve_source(path)
    if source is None:
        log.debug("upstream stats path %s has no source mapping", " / ".join(path))
        return None

    builder_kind = _KIND_DISPATCH.get(kind)
    if builder_kind == "text":
        return _build_text_widget(path, leaf, source, font_root)
    if builder_kind == "progress_bar":
        return _build_progress_bar(path, leaf, source)
    if builder_kind == "radial":
        return _build_radial(path, leaf, source, font_root)
    if builder_kind == "line_graph":
        return _build_line_graph(path, leaf, source, font_root)
    return None


def _walk_stats(stats: Any, path: tuple, font_root) -> List[Dict[str, Any]]:
    """Recursively walk the STATS hierarchy collecting widget dicts.

    Stops descending when it hits a known widget-kind leaf (e.g. TEXT,
    GRAPH, RADIAL, LINE_GRAPH). Everything else is treated as an
    intermediate node and we keep recursing.
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(stats, dict):
        return out
    for k, v in stats.items():
        if not isinstance(v, dict):
            continue
        key = str(k).upper()
        sub_path = path + (key,)
        if key in _WIDGET_KINDS:
            w = _build_widget_from_leaf(sub_path, v, font_root)
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
