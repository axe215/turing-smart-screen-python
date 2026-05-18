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

from .data_sources import DataSourceRegistry
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
    ):
        self.theme = theme
        self.sources = data_sources
        self.font_scale = max(0.1, float(font_scale))
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
        # Design surface: theme's canvas (landscape) with transparent bg
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
        """Render value + unit using the unified black/white-stroke style.

        Kept as a separate path even though it now matches _render_text
        in colors — the two-draw approach lets us position the unit
        right after the value without manually concatenating (and lets
        future themes color them differently via YAML overrides).
        """
        font = self._load_font(w.font)

        fill = tuple(w.raw["fill_color"]) if "fill_color" in w.raw else (0, 0, 0, 255)
        stroke = (
            tuple(w.raw["stroke_color"])
            if "stroke_color" in w.raw
            else (255, 255, 255, 255)
        )
        stroke_width = int(w.raw.get("stroke_width", 1))

        # Draw value
        try:
            draw.text(
                (w.x, w.y), value, font=font, fill=fill,
                stroke_width=stroke_width, stroke_fill=stroke,
            )
        except TypeError:
            draw.text((w.x, w.y), value, font=font, fill=fill)

        # Measure where the value ends and draw the unit right after it
        try:
            value_width = draw.textlength(value, font=font)
        except AttributeError:
            bbox = draw.textbbox((w.x, w.y), value, font=font)
            value_width = bbox[2] - bbox[0]

        unit_x = int(w.x + value_width)
        try:
            draw.text(
                (unit_x, w.y), unit, font=font, fill=fill,
                stroke_width=stroke_width, stroke_fill=stroke,
            )
        except TypeError:
            draw.text((unit_x, w.y), unit, font=font, fill=fill)

    def _render_text(self, draw: ImageDraw.ImageDraw, w: WidgetSpec, text: str):
        if not text:
            return
        font = self._load_font(w.font)

        # Unified style: all widget text (labels and live values) is
        # BLACK with a WHITE stroke. Black-on-white reads cleanly over
        # nearly any video frame, and the consistent treatment makes
        # the overlay feel like one coherent UI rather than mixed.
        # Per-widget YAML override still wins (fill_color/stroke_color/
        # stroke_width).
        fill = tuple(w.raw["fill_color"]) if "fill_color" in w.raw else (0, 0, 0, 255)
        stroke_color = (
            tuple(w.raw["stroke_color"])
            if "stroke_color" in w.raw
            else (255, 255, 255, 255)
        )
        stroke_width = int(w.raw.get("stroke_width", 1))

        try:
            draw.text(
                (w.x, w.y),
                text,
                font=font,
                fill=fill,
                stroke_width=stroke_width,
                stroke_fill=stroke_color,
            )
        except TypeError:
            # Older Pillow without stroke_* kwargs — fall back to plain text
            draw.text((w.x, w.y), text, font=font, fill=fill)

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

        # Colors (with sensible defaults that match the .turtheme style)
        border_c = tuple(w.border_color) if w.border_color else (255, 255, 255, 255)
        bg_c = tuple(w.fill_color) if w.fill_color else (0, 0, 0, 40)
        bar_c = tuple(w.line_color) if w.line_color else (255, 255, 255, 255)

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
            draw.rectangle(
                [bar_x, bar_top, bar_x + column_width - 1, y1 - 1],
                fill=bar_c,
            )

        # Border on top so bars don't bleed past the frame
        if w.border_width > 0:
            draw.rectangle(
                [x0, y0, x1, y1],
                outline=border_c,
                width=int(w.border_width),
            )

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
