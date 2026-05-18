"""Render a ThemeRuntime + live data into a full-screen RGBA frame.

The theme is designed in landscape (1920×480 for eva.rei). The screen
takes images in portrait native (480×1920 for 8.8", 462×1920 for 9.2").
We render to the landscape design canvas, then rotate to portrait at
the end, optionally cropping to 462 wide for the 9.2".
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

from collections import deque
from typing import Deque

from PIL import Image, ImageDraw, ImageFont

from .data_sources import DataSourceRegistry, DEFAULT_MIN_SIZE
from .runtime import FontSpec, ThemeRuntime, WidgetSpec

log = logging.getLogger(__name__)


# Native portrait dimensions per device generation
SCREEN_NATIVE = {
    "8.8": (480, 1920),
    "9.2": (462, 1920),
}

# When a font family from the theme isn't bundled, try these alternates
# (in order) before falling back to PIL's default raster font. Visually
# close substitutes for fonts that UsbMonitorL theme authors typically
# pull in from their system rather than bundling. Tweak per-theme via
# YAML font_aliases if you have a closer match.
FONT_FALLBACKS = {
    # Digital Dismay is the UsbMonitorL author's font of choice for big
    # numeric readouts (CPU/GPU temp, FPS). Prefer narrow LCD-styled
    # substitutes from the bundle — wider grunge fonts cause widgets to
    # overflow their column and visually collide with neighbours.
    "Digital Dismay": ["DS-Digital", "LCD", "Liquid Crystal", "Motorblock", "Kamikaze"],
    "LCD-Dismay": ["DS-Digital", "LCD", "Liquid Crystal"],
    # Osaka Japan (used for CPU/GPU model names and the clock) is a
    # stylized Japanese-flavored font that reads poorly at small sizes
    # for Latin model strings like "AMD64 Family 25 Model 97 ...".
    # Swap in a clean sans-serif from the bundle.
    "Osaka Japan": [
        "HarmonyOS Sans", "HarmonyOS Sans Bold",
        "Kumbh Sans", "Helvetica Neue LT Pro",
        "Helvetica Neue", "Arial",
    ],
}


class WidgetRenderer:
    def __init__(
        self,
        theme: ThemeRuntime,
        data_sources: DataSourceRegistry,
        screen: str = "9.2",
        font_scale: float = 1.0,
        background_image: Optional[Image.Image] = None,
        force_black_text: bool = False,
    ):
        self.theme = theme
        self.sources = data_sources
        self.font_scale = max(0.1, float(font_scale))
        # When True, the renderer overrides each widget's font.color
        # with BLACK + 1-px WHITE stroke. Designed for video-mode
        # themes where any light pixel in the underlying video could
        # swallow the theme's intended (often white) text.
        self.force_black_text = bool(force_black_text)
        # When set, render_frame composes widgets ONTO this image
        # (canvas-sized RGBA), producing an opaque frame ready to send
        # via cmd 102 without a streaming video underneath.
        # When None, the engine is in video mode: produce a transparent
        # overlay; the screen composes it over the streaming H.264.
        self.background_image: Optional[Image.Image] = None
        if background_image is not None:
            # Pre-fit the background to the design canvas exactly so
            # widget coordinates line up regardless of the source size.
            bg = background_image.convert("RGBA")
            if bg.size != (theme.canvas.width, theme.canvas.height):
                bg = bg.resize(
                    (theme.canvas.width, theme.canvas.height),
                    Image.LANCZOS,
                )
            self.background_image = bg
        self.font_cache: Dict[Tuple[str, int, bool], ImageFont.ImageFont] = {}
        self.image_cache: Dict[Path, Image.Image] = {}
        self.native_w, self.native_h = SCREEN_NATIVE.get(screen, SCREEN_NATIVE["9.2"])
        self.design_w = theme.canvas.width
        self.design_h = theme.canvas.height
        # Build a {family_name → font_file_path} index by reading every
        # TTF/OTF in theme/fonts/ once at startup. PIL system-lookup is
        # OS-dependent and unreliable on Windows without the font being
        # registered; scanning the theme's bundled fonts is robust.
        self.font_family_index: Dict[str, Path] = self._build_font_index()
        log.info(
            "font index for %s: %d families found",
            theme.name,
            len(self.font_family_index),
        )
        # Per-widget rolling history for Chart widgets. Keyed by widget.id,
        # size is widget-specific (computed from width / column_width).
        self.chart_history: Dict[str, Deque[float]] = {}

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def render_frame(self, rotate_180: bool = False) -> Image.Image:
        """Build a full-screen RGBA PIL image ready to send via cmd 102.

        Output is in **portrait native** orientation expected by the
        firmware (native_w × native_h).
        """
        # Design surface: either start from the static background image
        # (image-mode themes) or a transparent canvas that the streaming
        # video will compose under (video-mode themes).
        if self.background_image is not None:
            canvas = self.background_image.copy()
        else:
            canvas = Image.new("RGBA", (self.design_w, self.design_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)

        for w in self.theme.widgets:
            if w.hide or not w.enabled:
                continue
            try:
                self._render_widget(canvas, draw, w)
            except Exception as exc:
                log.warning("Failed to render widget %s: %s", w.id, exc)

        # Convert landscape (design_w × design_h) → portrait (h × w)
        # by rotating 90° clockwise (Pillow ROTATE_270 is CCW so we use ROTATE_90)
        portrait = canvas.transpose(Image.Transpose.ROTATE_270)

        # Crop to native width — for 9.2" this trims 18px from the long edge
        if portrait.width > self.native_w:
            # Crop centered horizontally (so the layout's middle stays in frame)
            x_off = (portrait.width - self.native_w) // 2
            portrait = portrait.crop((x_off, 0, x_off + self.native_w, portrait.height))
        # If the native height differs (it shouldn't for 1920), pad/crop too
        if portrait.height > self.native_h:
            portrait = portrait.crop((0, 0, portrait.width, self.native_h))
        elif portrait.height < self.native_h:
            padded = Image.new("RGBA", (portrait.width, self.native_h), (0, 0, 0, 0))
            padded.paste(portrait, (0, 0))
            portrait = padded

        if rotate_180:
            portrait = portrait.transpose(Image.Transpose.ROTATE_180)

        return portrait

    # ------------------------------------------------------------------
    # Per-widget rendering
    # ------------------------------------------------------------------

    def _render_widget(self, canvas: Image.Image, draw: ImageDraw.ImageDraw, w: WidgetSpec):
        if w.type == "data":
            self._render_data(draw, w)
        elif w.type == "text":
            self._render_text(draw, w, w.text)
        elif w.type == "image":
            self._render_image(canvas, w)
        elif w.type == "chart":
            self._render_chart(draw, w)
        elif w.type == "progress_bar":
            self._render_progress_bar(draw, w)
        elif w.type == "radial":
            self._render_radial(canvas, draw, w)
        elif w.type == "line_graph":
            self._render_line_graph(draw, w)
        else:
            log.debug("Skipping widget %s (unknown type %s)", w.id, w.type)

    def _render_data(self, draw: ImageDraw.ImageDraw, w: WidgetSpec):
        fn = self.sources.get(w.source)
        try:
            result = fn(w.show_unit)
        except Exception as exc:
            log.warning("Source %s failed: %s", w.source, exc)
            result = ("", "")
        # Sources now return (value_str, unit_str). Backwards-compat with
        # plain strings if someone wires a custom source the old way.
        if isinstance(result, tuple):
            value_part, unit_part = result
        else:
            value_part, unit_part = (result, "")
        if not value_part and not unit_part:
            return
        # Right-pad to min_size — matches upstream mathoudebine layout,
        # where percent/temp values are formatted "%>3" so themes can
        # position widgets assuming a fixed 3-character field. Per-widget
        # `min_size` overrides the per-source default.
        ms = int(w.raw.get("min_size", DEFAULT_MIN_SIZE.get(w.source, 0)))
        if ms > 0 and value_part:
            value_part = f"{value_part:>{ms}}"
        # Render the value in widget color, then the unit in inverted
        # color immediately after — same style as Text widgets so units
        # like "GB" / "W" / "%" pop visually against the bright numbers.
        if unit_part:
            self._render_data_with_unit(draw, w, value_part, unit_part)
        else:
            self._render_text(draw, w, value_part)

    def _render_data_with_unit(
        self,
        draw: ImageDraw.ImageDraw,
        w: WidgetSpec,
        value: str,
        unit: str,
    ):
        """Render value + unit using the theme's font color."""
        font = self._load_font(w.font)

        if "fill_color" in w.raw:
            fill = tuple(w.raw["fill_color"])
        elif self.force_black_text:
            fill = (0, 0, 0, 255)
        elif w.font is not None:
            fill = w.font.color
        else:
            fill = (255, 255, 255, 255)
        if "stroke_color" in w.raw:
            stroke = tuple(w.raw["stroke_color"])
        elif self.force_black_text:
            stroke = (255, 255, 255, 255)
        else:
            stroke = (0, 0, 0, 255)
        default_stroke_w = 1 if self.force_black_text else 0
        stroke_width = int(w.raw.get("stroke_width", default_stroke_w))

        # Draw value. anchor="lt" matches upstream mathoudebine's default
        # (Pillow without `anchor` uses "la" = baseline of ascender, which
        # places text a few pixels lower than the theme's authors expect).
        try:
            draw.text(
                (w.x, w.y), value, font=font, fill=fill, anchor="lt",
                stroke_width=stroke_width, stroke_fill=stroke,
            )
        except TypeError:
            draw.text((w.x, w.y), value, font=font, fill=fill, anchor="lt")

        # Measure where the value ends and draw the unit right after it
        try:
            value_width = draw.textlength(value, font=font)
        except AttributeError:
            bbox = draw.textbbox((w.x, w.y), value, font=font, anchor="lt")
            value_width = bbox[2] - bbox[0]

        unit_x = int(w.x + value_width)
        try:
            draw.text(
                (unit_x, w.y), unit, font=font, fill=fill, anchor="lt",
                stroke_width=stroke_width, stroke_fill=stroke,
            )
        except TypeError:
            draw.text((unit_x, w.y), unit, font=font, fill=fill, anchor="lt")

    def _render_text(self, draw: ImageDraw.ImageDraw, w: WidgetSpec, text: str):
        if not text:
            return
        font = self._load_font(w.font)

        # Color resolution order:
        #   1. YAML per-widget override (fill_color / stroke_color)
        #   2. force_black_text mode → black fill, white stroke
        #   3. theme's font.color (default — respects what the theme designer set)
        if "fill_color" in w.raw:
            fill = tuple(w.raw["fill_color"])
        elif self.force_black_text:
            fill = (0, 0, 0, 255)
        elif w.font is not None:
            fill = w.font.color
        else:
            fill = (255, 255, 255, 255)

        if "stroke_color" in w.raw:
            stroke_color = tuple(w.raw["stroke_color"])
        elif self.force_black_text:
            stroke_color = (255, 255, 255, 255)
        else:
            stroke_color = (0, 0, 0, 255)

        default_stroke_w = 1 if self.force_black_text else 0
        stroke_width = int(w.raw.get("stroke_width", default_stroke_w))

        # anchor="lt" matches upstream mathoudebine's default placement,
        # where (X, Y) is the top-left of the text bounding box. Pillow's
        # default ("la") uses the ascender top instead, leaving extra
        # leading above the glyphs and visually pushing text down.
        try:
            draw.text(
                (w.x, w.y),
                text,
                font=font,
                fill=fill,
                anchor="lt",
                stroke_width=stroke_width,
                stroke_fill=stroke_color,
            )
        except TypeError:
            # Older Pillow without stroke_* kwargs — fall back to plain text
            draw.text((w.x, w.y), text, font=font, fill=fill, anchor="lt")

    def _render_image(self, canvas: Image.Image, w: WidgetSpec):
        if not w.image:
            return
        img_path = self.theme.image_path(w.image)
        if not img_path.exists():
            log.warning("Image %s not found at %s", w.image, img_path)
            return
        cached = self.image_cache.get(img_path)
        if cached is None:
            try:
                cached = Image.open(img_path).convert("RGBA")
            except Exception as exc:
                log.warning("Failed to open image %s: %s", img_path, exc)
                return
            self.image_cache[img_path] = cached
        img = cached
        if abs(w.scale - 1.0) > 1e-6 and w.scale > 0:
            new_w = max(1, int(img.width * w.scale))
            new_h = max(1, int(img.height * w.scale))
            img = img.resize((new_w, new_h), Image.LANCZOS)
        canvas.alpha_composite(img, dest=(w.x, w.y))

    def _render_chart(self, draw: ImageDraw.ImageDraw, w: WidgetSpec):
        """Column-style chart with rolling history.

        Layout (right-to-left, newest sample on the right):
          - canvas rectangle filled with `fill_color` (semi-transparent
            tint so the video shows through)
          - vertical bars colored `line_color`, height proportional to
            sample / max_value
          - 1px border around the chart in `border_color`

        History size auto-derived from width / column_width. column_width
        defaults to 5 (matches UsbMonitorL eva.rei chart definition).
        """
        if w.width <= 0 or w.height <= 0:
            return

        column_width = max(1, int(w.raw.get("column_width", 5)))
        max_bars = max(1, w.width // column_width)
        # Track history per widget id; trim on resize when YAML changes
        hist = self.chart_history.get(w.id)
        if hist is None or hist.maxlen != max_bars:
            hist = deque(maxlen=max_bars)
            self.chart_history[w.id] = hist

        # Pull current numeric value; missing → 0 so the chart still scrolls
        val = self.sources.get_numeric(w.source)
        hist.append(val if val is not None else 0.0)

        # Colors. Unified with the text-style choice: BLACK fill with
        # a thin WHITE outline, so each bar is clearly visible against
        # any video frame underneath.
        border_c = tuple(w.border_color) if w.border_color else (255, 255, 255, 255)
        bg_c = tuple(w.fill_color) if w.fill_color else (0, 0, 0, 40)
        bar_fill = tuple(w.raw["bar_color"]) if "bar_color" in w.raw else (0, 0, 0, 255)
        bar_stroke = (
            tuple(w.raw["bar_stroke_color"])
            if "bar_stroke_color" in w.raw
            else (255, 255, 255, 255)
        )

        x0, y0 = w.x, w.y
        x1, y1 = w.x + w.width, w.y + w.height

        # Background panel (semi-transparent so video shows through)
        draw.rectangle([x0, y0, x1, y1], fill=bg_c)

        # Bars: rightmost is newest. Walk history in reverse to place from right edge.
        max_value = max(1e-6, float(w.max_value))
        for i, sample in enumerate(reversed(hist)):
            bar_x = x1 - (i + 1) * column_width
            if bar_x < x0:
                break  # ran out of room (shouldn't happen given maxlen=max_bars)
            scaled = max(0.0, min(float(sample) / max_value, 1.0))
            bar_h = int(scaled * w.height)
            if bar_h <= 0:
                continue
            bar_top = y1 - bar_h
            # Black fill + 1px white outline. column_width is usually 5,
            # so the outline visually frames each bar without consuming
            # too much of the column.
            draw.rectangle(
                [bar_x, bar_top, bar_x + column_width - 1, y1 - 1],
                fill=bar_fill,
                outline=bar_stroke,
                width=1,
            )

        # Border on top so bars don't bleed past the frame
        if w.border_width > 0:
            draw.rectangle(
                [x0, y0, x1, y1],
                outline=border_c,
                width=int(w.border_width),
            )

    # ------------------------------------------------------------------
    # Upstream-flavored widgets: progress_bar / radial / line_graph
    # ------------------------------------------------------------------

    def _render_progress_bar(self, draw: ImageDraw.ImageDraw, w: WidgetSpec):
        """Horizontal bar: background rect + filled portion proportional
        to (value − min) / (max − min). Mirrors upstream GRAPH semantics
        — including REVERSE_DIRECTION (fill grows right→left)."""
        if w.width <= 0 or w.height <= 0:
            return
        min_v = float(w.raw.get("min_value", 0))
        max_v = float(w.raw.get("max_value", 100))
        span = max_v - min_v
        if span <= 0:
            return
        val = self.sources.get_numeric(w.source)
        if val is None:
            val = min_v
        norm = max(0.0, min(1.0, (float(val) - min_v) / span))
        reverse = bool(w.raw.get("reverse_direction", False))

        x0, y0 = w.x, w.y
        x1, y1 = w.x + w.width, w.y + w.height

        bg = w.raw.get("background_color")
        if bg:
            draw.rectangle([x0, y0, x1, y1], fill=tuple(bg))

        bar_c = tuple(w.raw.get("bar_color", (0, 255, 0, 255)))
        fill_px = int(round(norm * w.width))
        if fill_px > 0:
            if reverse:
                draw.rectangle([x1 - fill_px, y0, x1, y1], fill=bar_c)
            else:
                draw.rectangle([x0, y0, x0 + fill_px, y1], fill=bar_c)

        if w.raw.get("bar_outline"):
            draw.rectangle([x0, y0, x1, y1], outline=bar_c, width=1)

    def _render_radial(self, canvas: Image.Image, draw: ImageDraw.ImageDraw, w: WidgetSpec):
        """Radial progress dial. Center at (x, y), arc swept from
        ANGLE_START to ANGLE_END based on value%.

        Upstream uses standard math angle convention (0° east, CCW), but
        Pillow's ImageDraw.arc uses 0° east, clockwise. CLOCKWISE: True
        in the theme means "fill builds clockwise from start" — we
        translate by swapping start/end when CLOCKWISE is False.
        """
        radius = int(w.raw.get("radius", 0))
        if radius <= 0:
            return
        min_v = float(w.raw.get("min_value", 0))
        max_v = float(w.raw.get("max_value", 100))
        span = max_v - min_v
        if span <= 0:
            return
        val = self.sources.get_numeric(w.source)
        if val is None:
            val = min_v
        norm = max(0.0, min(1.0, (float(val) - min_v) / span))

        cx, cy = w.x, w.y
        thickness = max(1, int(w.raw.get("width", 10)))
        a_start = float(w.raw.get("angle_start", 0.0))
        a_end = float(w.raw.get("angle_end", 360.0))
        clockwise = bool(w.raw.get("clockwise", True))

        # Compute total sweep based on direction. Pillow draws arcs CW.
        if clockwise:
            sweep = (a_end - a_start) % 360
            if sweep == 0:
                sweep = 360
            filled_end = a_start + sweep * norm
            arc_start, arc_end = a_start, filled_end
        else:
            sweep = (a_start - a_end) % 360
            if sweep == 0:
                sweep = 360
            filled_end = a_start - sweep * norm
            arc_start, arc_end = filled_end, a_start

        bbox = [cx - radius, cy - radius, cx + radius, cy + radius]
        bar_c = tuple(w.raw.get("bar_color", (0, 255, 0, 255)))
        try:
            draw.arc(bbox, start=arc_start, end=arc_end, fill=bar_c, width=thickness)
        except TypeError:
            # Older Pillow without `width` kwarg — fall back to thin arc
            draw.arc(bbox, start=arc_start, end=arc_end, fill=bar_c)

        if w.raw.get("show_text"):
            # Centered numeric readout
            show_unit = bool(w.raw.get("show_unit", False))
            fn = self.sources.get(w.source)
            try:
                value_part, unit_part = fn(show_unit)
            except Exception:
                value_part, unit_part = (str(int(val)), "")
            label = (value_part + unit_part) if show_unit else value_part
            if label:
                font = self._load_font(w.font)
                fill = w.font.color if (w.font is not None and not self.force_black_text) else (
                    (0, 0, 0, 255) if self.force_black_text else (255, 255, 255, 255)
                )
                try:
                    draw.text((cx, cy), label, font=font, fill=fill, anchor="mm")
                except TypeError:
                    # Older Pillow without anchor support — fall back to manual centering
                    try:
                        tw = draw.textlength(label, font=font)
                    except AttributeError:
                        bb = draw.textbbox((0, 0), label, font=font)
                        tw = bb[2] - bb[0]
                    th = font.size if hasattr(font, "size") else 12
                    draw.text((cx - tw / 2, cy - th / 2), label, font=font, fill=fill)

    def _render_line_graph(self, draw: ImageDraw.ImageDraw, w: WidgetSpec):
        """Line plot of rolling samples. History deque keyed by widget id.

        Maps the y-axis: sample value → pixel y inside [w.y, w.y+h].
        autoscale=True picks min/max from current history (excluding
        empty); otherwise uses the theme-supplied MIN_VALUE/MAX_VALUE.
        """
        if w.width <= 0 or w.height <= 0:
            return
        history_size = max(2, int(w.raw.get("history_size", 60)))
        hist = self.chart_history.get(w.id)
        if hist is None or hist.maxlen != history_size:
            hist = deque(maxlen=history_size)
            self.chart_history[w.id] = hist
        val = self.sources.get_numeric(w.source)
        hist.append(float(val) if val is not None else 0.0)

        x0, y0 = w.x, w.y
        x1, y1 = w.x + w.width, w.y + w.height

        bg = w.raw.get("background_color")
        if bg:
            draw.rectangle([x0, y0, x1, y1], fill=tuple(bg))

        if w.raw.get("autoscale"):
            samples = list(hist)
            if samples:
                min_v, max_v = min(samples), max(samples)
                if max_v - min_v < 1e-6:
                    max_v = min_v + 1
            else:
                min_v, max_v = 0.0, 1.0
        else:
            min_v = float(w.raw.get("min_value", 0))
            max_v = float(w.raw.get("max_value", 100))
            if max_v - min_v < 1e-6:
                max_v = min_v + 1

        # Plot polyline. With N samples in history, lay them across the
        # full width. Newest sample on the right.
        n = len(hist)
        if n >= 2:
            line_c = tuple(w.raw.get("line_color", (255, 255, 255, 255)))
            line_w = max(1, int(w.raw.get("line_width", 1)))
            pts = []
            for i, s in enumerate(hist):
                # left-to-right: oldest at x0, newest at x1
                px = x0 + (w.width * i) / (history_size - 1) if history_size > 1 else x0
                norm = (float(s) - min_v) / (max_v - min_v)
                norm = max(0.0, min(1.0, norm))
                py = y1 - norm * w.height
                pts.append((px, py))
            try:
                draw.line(pts, fill=line_c, width=line_w, joint="curve")
            except TypeError:
                draw.line(pts, fill=line_c, width=line_w)

        if w.raw.get("axis"):
            axis_c = tuple(w.raw.get("axis_color", (255, 255, 255, 255)))
            # Bottom + left axes
            draw.line([(x0, y1), (x1, y1)], fill=axis_c, width=1)
            draw.line([(x0, y0), (x0, y1)], fill=axis_c, width=1)
            axis_font_spec = w.raw.get("axis_font") or {}
            if axis_font_spec:
                from .runtime import FontSpec
                fs = FontSpec.from_dict(axis_font_spec)
                font = self._load_font(fs)
                # Min label at bottom-left, max label at top-left (just inside the axis)
                draw.text((x0 + 2, y1 - 12), f"{int(min_v)}", font=font, fill=axis_c)
                draw.text((x0 + 2, y0 + 2), f"{int(max_v)}", font=font, fill=axis_c)

    # ------------------------------------------------------------------
    # Font loading
    # ------------------------------------------------------------------

    def _build_font_index(self) -> Dict[str, Path]:
        """Scan <theme_dir>/fonts/ once and build a family-name → path map.

        Uses PIL's ImageFont.getname() which reads the TTF/OTF name table
        for the canonical family name. Robust against arbitrary filenames
        (e.g. "DOTMATRI.TTF" has family "Dot Matrix").
        """
        index: Dict[str, Path] = {}
        fonts_dir = self.theme.theme_dir / "fonts"
        if not fonts_dir.exists() or not fonts_dir.is_dir():
            return index
        for f in fonts_dir.iterdir():
            if not f.is_file():
                continue
            if f.suffix.lower() not in (".ttf", ".otf", ".ttc"):
                continue
            try:
                # Open at a small size just to read the name table
                ft = ImageFont.truetype(str(f), 12)
                family, _style = ft.getname()
            except Exception as exc:
                log.debug("could not read font name from %s: %s", f, exc)
                continue
            if family and family not in index:
                index[family] = f
        return index

    def _load_font(self, spec: Optional[FontSpec]) -> ImageFont.ImageFont:
        if spec is None or not spec.family:
            return ImageFont.load_default()
        # Apply global font-size scaling. Minimum 6px so we never collapse
        # a small label to a degenerate size.
        scaled_size = max(6, int(round(spec.size * self.font_scale)))
        key = (spec.family, scaled_size, spec.bold)
        cached = self.font_cache.get(key)
        if cached is not None:
            return cached

        # Build the list of family names to try, in order
        candidates = [spec.family]
        candidates += [spec.family.replace(" ", ""), spec.family.replace(" ", "_")]
        # Yaml-defined per-theme aliases would go here in a future version.
        # For now use built-in FONT_FALLBACKS for known-missing fonts.
        candidates += FONT_FALLBACKS.get(spec.family, [])

        font = None
        # 1) Try each candidate via theme's font index built from name tables
        for fam in candidates:
            path = self.font_family_index.get(fam)
            if path is not None:
                try:
                    font = ImageFont.truetype(str(path), scaled_size)
                    if fam != spec.family:
                        log.info(
                            "Font %s not found; substituted %s from theme fonts",
                            spec.family,
                            fam,
                        )
                    break
                except OSError:
                    pass
        # 3) Try filename-based lookup (legacy paths)
        if font is None:
            for ext in (".ttf", ".otf", ".TTF", ".OTF"):
                for variant in (
                    spec.family,
                    spec.family.replace(" ", ""),
                    spec.family.replace(" ", "_"),
                ):
                    candidate = self.theme.theme_dir / "fonts" / f"{variant}{ext}"
                    if candidate.exists():
                        try:
                            font = ImageFont.truetype(str(candidate), scaled_size)
                            break
                        except OSError:
                            pass
                if font is not None:
                    break
        # 4) Try PIL system lookup by family name (Windows-registered fonts)
        if font is None:
            try:
                font = ImageFont.truetype(spec.family, scaled_size)
            except OSError:
                pass
        # 5) Default fallback
        if font is None:
            log.warning(
                "Font %s not found in %s/fonts/ — using default",
                spec.family,
                self.theme.theme_dir,
            )
            font = ImageFont.load_default()

        self.font_cache[key] = font
        return font
