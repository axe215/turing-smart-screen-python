"""Parse UsbMonitorL .turtheme files (.NET BinaryFormatter) into Python dataclasses.

Usage:
    from library.turtheme import parse_turtheme
    theme = parse_turtheme("EVAREI.turtheme")
    print(theme.name, theme.width, theme.height)
    for w in theme.widgets:
        print(w.type_name, w.display_name, w.x, w.y)

This module uses the vendored nrbf.py (from gurnec/Undo_FFG) as the
low-level NRBF reader, then walks the resulting object graph to extract
the fields that matter to us.

UsbMonitorL stores all auto-properties as `<name>k__BackingField`
(standard C# autogen). We unmangle those when reading.

Widget types found in real themes:
  - "Image"  → GraphImage class, has bitmap + image name
  - "Data"   → live monitoring value (CPU/GPU temp, usage, etc.)
  - "Text"   → static label text
  - "Chart"  → line/bar chart (GraphLine class) with width/height + colors
"""
from __future__ import annotations

import collections
import collections.abc
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


# --- collections shim for Python 3.10+ ---
# nrbf.py depends on the old `namedlist` package which still references
# collections.Mapping / collections.Sequence (moved to collections.abc in 3.10).
for _name in ("Mapping", "Sequence", "Iterable", "MutableMapping", "MutableSequence"):
    if not hasattr(collections, _name):
        setattr(collections, _name, getattr(collections.abc, _name))

from . import nrbf  # noqa: E402

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Color:
    """A System.Drawing.Color decoded into RGBA.

    .NET System.Drawing.Color is opaque — when constructed from RGB it
    stores the ARGB packed into `value` (uint64) with `state=2`. When
    it's a "known" color from the .NET enum, only `knownColor` is set.
    The default (state=0, value=0, knownColor=0) is treated as "empty".
    """

    r: int
    g: int
    b: int
    a: int = 255
    is_default: bool = False  # True when source was the .NET "empty" Color

    @classmethod
    def from_system_drawing(cls, sd_color) -> "Color":
        """Convert a parsed System.Drawing.Color named-list into RGBA."""
        if sd_color is None:
            return cls(0, 0, 0, 255, is_default=True)
        # Use _asdict to avoid name collisions in field access
        d = sd_color._asdict()
        value = d.get("value", 0)
        known = d.get("knownColor", 0)
        state = d.get("state", 0)
        # value holds packed ARGB when state=2 (StateValueMask)
        # but in practice we just look at non-zero packed value.
        if value:
            a = (value >> 24) & 0xFF
            r = (value >> 16) & 0xFF
            g = (value >> 8) & 0xFF
            b = value & 0xFF
            return cls(r, g, b, a)
        if known:
            # Without an enum table we fall back to "white" for known colors.
            # In practice UsbMonitorL themes rarely use named colors.
            return cls(255, 255, 255, 255)
        return cls(0, 0, 0, 255, is_default=True)

    def to_tuple_rgb(self) -> Tuple[int, int, int]:
        return (self.r, self.g, self.b)

    def to_tuple_rgba(self) -> Tuple[int, int, int, int]:
        return (self.r, self.g, self.b, self.a)


@dataclass(frozen=True)
class Alignment:
    """UsbMonitorL.TextAlignment — horizontal + vertical."""

    horizontal: int = 0  # 0=left, 1=center, 2=right (educated guess; verify)
    vertical: int = 0

    @classmethod
    def from_nrbf(cls, alignment) -> "Alignment":
        if alignment is None:
            return cls()
        d = alignment._asdict()
        # Field names vary; pull defensively
        return cls(
            horizontal=int(d.get("horizontalAlignment_k__BackingField", d.get("horizontal", 0)) or 0),
            vertical=int(d.get("verticalAlignment_k__BackingField", d.get("vertical", 0)) or 0),
        )


@dataclass
class FontConfig:
    name: str = ""
    size: int = 12
    is_bold: bool = False
    color: Color = field(default_factory=lambda: Color(255, 255, 255))
    grad_color: Color = field(default_factory=lambda: Color(255, 255, 255))
    grad_direction: int = 0
    alignment: Alignment = field(default_factory=Alignment)
    interval: float = 0.0

    @classmethod
    def from_nrbf(cls, fc) -> "FontConfig":
        if fc is None:
            return cls()
        d = fc._asdict()
        return cls(
            name=d.get("name_k__BackingField", "") or "",
            size=int(d.get("size_k__BackingField", 12) or 12),
            is_bold=bool(d.get("isBold_k__BackingField", False)),
            color=Color.from_system_drawing(d.get("color_k__BackingField")),
            grad_color=Color.from_system_drawing(d.get("GrColor_k__BackingField")),
            grad_direction=int(d.get("GrDirection_k__BackingField", 0) or 0),
            alignment=Alignment.from_nrbf(d.get("alignment_k__BackingField")),
            interval=float(d.get("interval_k__BackingField", 0.0) or 0.0),
        )


@dataclass
class WidgetDef:
    """Common base for every widget on the theme canvas."""

    type_name: str
    display_name: str
    x: int
    y: int
    sub_type: Optional[str] = None
    hide: bool = False
    enabled: bool = False
    use_gradient: bool = False
    revert: bool = False


@dataclass
class DataWidget(WidgetDef):
    """Live data widget bound to a hardware sensor."""

    data_name: str = ""
    sanma_eng_name: str = ""
    sub_name: Optional[str] = None
    show_unit: bool = False
    font: Optional[FontConfig] = None


@dataclass
class TextWidget(WidgetDef):
    """Static label / decoration text."""

    text: str = ""
    font: Optional[FontConfig] = None


@dataclass
class ChartWidget(WidgetDef):
    """Line/bar chart widget."""

    width: int = 0
    height: int = 0
    max_value: float = 100.0
    line_color: Color = field(default_factory=lambda: Color(255, 255, 255))
    fill_color: Color = field(default_factory=lambda: Color(255, 255, 255))
    border_color: Color = field(default_factory=lambda: Color(0, 0, 0))
    line_width: int = 1
    border_width: int = 1
    column_width: int = 5
    coefficient: float = 1.0
    roll_direction: bool = False
    data_name: str = ""


@dataclass
class ImageWidget(WidgetDef):
    """Static image / icon."""

    image_name: str = ""
    zoom_rate: float = 1.0
    bitmap_png: Optional[bytes] = None  # PNG bytes extracted from System.Drawing.Bitmap


@dataclass
class ThemeDef:
    name: str
    width: int
    height: int
    is_landscape: bool
    front_color: Color
    back_color: Color
    video_path_local: Optional[str]  # path UsbMonitorL had on the editor's PC
    video_target_path: Optional[str]  # path on the device (per UsbMonitorL)
    video_name: Optional[str]
    is_visual_theme: bool
    is_temp_theme: bool
    is_aida_theme: bool
    widgets: List[WidgetDef] = field(default_factory=list)
    raw_root: object = None  # the raw parsed object, kept for ad-hoc inspection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_BF_RE = re.compile(r"<(.+?)>k__BackingField")


def _unmangle(name: str) -> str:
    """Strip the <name>k__BackingField wrapper used for C# auto-properties."""
    m = _BF_RE.match(name)
    return m.group(1) if m else name


def _field(obj, *candidates, default=None):
    """Look up a field by any of several candidate names on a nrbf namedlist.

    The candidates can be either the mangled (`<x>k__BackingField`) or plain
    form — we look both in `obj._asdict()` for direct hits and via _unmangle.
    """
    d = obj._asdict()
    direct = dict(d)
    unmangled = {_unmangle(k): v for k, v in d.items()}
    for c in candidates:
        if c in direct:
            return direct[c]
        if c in unmangled:
            return unmangled[c]
        # Auto-property form
        bf = f"<{c}>k__BackingField"
        if bf in direct:
            return direct[bf]
        # Some collection fields have a different prefix on subclass items
        for k, v in direct.items():
            if k.endswith(f"_{c}") or k.endswith(f"_<{c}>k__BackingField"):
                return v
    return default


def _extract_bitmap_png(bitmap_obj) -> Optional[bytes]:
    """Try to pull PNG bytes out of a System.Drawing.Bitmap nrbf object.

    Real-world .turtheme dumps store the image as the raw bytes of the
    original file (PNG/JPG); the Bitmap object usually contains a single
    `Data` byte array.
    """
    if bitmap_obj is None:
        return None
    try:
        d = bitmap_obj._asdict()
    except AttributeError:
        return None
    # Look for any field whose value is a bytes-like / bytearray / list of ints
    for k, v in d.items():
        if isinstance(v, (bytes, bytearray)):
            return bytes(v)
        # nrbf may decode byte[] to array.array('b') or list of ints
        if hasattr(v, "tobytes"):
            try:
                return v.tobytes()
            except Exception:
                pass
        if isinstance(v, list) and v and isinstance(v[0], int):
            try:
                return bytes(v)
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# Item walker
# ---------------------------------------------------------------------------


def _parse_image_widget(item) -> ImageWidget:
    bm_obj = _field(item, "bitmap_k__BackingField", "bitmap")
    return ImageWidget(
        type_name=_field(item, "GraphItem_<TypeName>k__BackingField", "TypeName_k__BackingField", "TypeName") or "Image",
        display_name=_field(item, "GraphItem_<DisplayName>", "GraphItem__DisplayName", "DisplayName") or "",
        x=int(_field(item, "GraphItem_<posX>k__BackingField", "GraphItem__posX_k__BackingField", "posX_k__BackingField", default=0) or 0),
        y=int(_field(item, "GraphItem_<posY>k__BackingField", "GraphItem__posY_k__BackingField", "posY_k__BackingField", default=0) or 0),
        sub_type=_field(item, "GraphItem_<SubTypeName>k__BackingField", "GraphItem__SubTypeName_k__BackingField", "SubTypeName_k__BackingField"),
        hide=bool(_field(item, "GraphItem_<hide>k__BackingField", "GraphItem__hide_k__BackingField", "hide_k__BackingField", default=False)),
        enabled=bool(_field(item, "GraphItem_<enabled>k__BackingField", "GraphItem__enabled_k__BackingField", "enabled_k__BackingField", default=False)),
        image_name=str(_field(item, "ImgName_k__BackingField", "ImgName") or ""),
        zoom_rate=float(_field(item, "zoom_rate_k__BackingField", "zoom_rate", default=1.0) or 1.0),
        bitmap_png=_extract_bitmap_png(bm_obj),
    )


def _parse_chart_widget(item) -> ChartWidget:
    md = _field(item, "GraphItem_<m_data>k__BackingField", "GraphItem__m_data_k__BackingField", "m_data_k__BackingField")
    data_name = ""
    if md is not None:
        data_name = str(_field(md, "DataName_k__BackingField", "DataName") or "")
    return ChartWidget(
        type_name=_field(item, "GraphItem_<TypeName>k__BackingField", "GraphItem__TypeName_k__BackingField", "TypeName_k__BackingField") or "Chart",
        display_name=_field(item, "GraphItem_<DisplayName>", "GraphItem__DisplayName", "DisplayName") or "",
        x=int(_field(item, "GraphItem_<posX>k__BackingField", "GraphItem__posX_k__BackingField", "posX_k__BackingField", default=0) or 0),
        y=int(_field(item, "GraphItem_<posY>k__BackingField", "GraphItem__posY_k__BackingField", "posY_k__BackingField", default=0) or 0),
        sub_type=_field(item, "GraphItem_<SubTypeName>k__BackingField", "GraphItem__SubTypeName_k__BackingField"),
        hide=bool(_field(item, "GraphItem_<hide>k__BackingField", "GraphItem__hide_k__BackingField", default=False)),
        enabled=bool(_field(item, "GraphItem_<enabled>k__BackingField", "GraphItem__enabled_k__BackingField", default=False)),
        width=int(_field(item, "width", default=0) or 0),
        height=int(_field(item, "height", default=0) or 0),
        max_value=float(_field(item, "maxValue_k__BackingField", default=100.0) or 100.0),
        line_color=Color.from_system_drawing(_field(item, "LineColor_k__BackingField")),
        fill_color=Color.from_system_drawing(_field(item, "FillColor_k__BackingField")),
        border_color=Color.from_system_drawing(_field(item, "BorderColor_k__BackingField")),
        line_width=int(_field(item, "lineWidth_k__BackingField", default=1) or 1),
        border_width=int(_field(item, "borderWidth_k__BackingField", default=1) or 1),
        column_width=int(_field(item, "columnWidth_k__BackingField", default=5) or 5),
        coefficient=float(_field(item, "coefficient_k__BackingField", default=1.0) or 1.0),
        roll_direction=bool(_field(item, "rollDirection_k__BackingField", default=False)),
        data_name=data_name,
    )


def _parse_data_or_text_widget(item):
    md = _field(item, "m_data_k__BackingField", "m_data")
    fc = _field(item, "fontConfig_k__BackingField", "fontConfig")
    font = FontConfig.from_nrbf(fc)
    type_name = _field(item, "TypeName_k__BackingField", "TypeName") or ""
    common_kwargs = dict(
        type_name=type_name,
        display_name=_field(item, "DisplayName") or "",
        x=int(_field(item, "posX_k__BackingField", "posX", default=0) or 0),
        y=int(_field(item, "posY_k__BackingField", "posY", default=0) or 0),
        sub_type=_field(item, "SubTypeName_k__BackingField", "SubTypeName"),
        hide=bool(_field(item, "hide_k__BackingField", "hide", default=False)),
        enabled=bool(_field(item, "enabled_k__BackingField", "enabled", default=False)),
        use_gradient=bool(_field(item, "useGradient_k__BackingField", "useGradient", default=False)),
        revert=bool(_field(item, "revert_k__BackingField", "revert", default=False)),
    )

    if type_name == "Text":
        # For Text widgets the displayed text is encoded into DisplayName as "Text--XYZ"
        text = ""
        dn = common_kwargs["display_name"]
        if dn and "--" in dn:
            text = dn.split("--", 1)[1]
        return TextWidget(**common_kwargs, text=text, font=font)

    # Otherwise it's a Data widget
    data_name = ""
    sanma = ""
    sub_name = None
    show_unit = False
    if md is not None:
        data_name = str(_field(md, "DataName_k__BackingField", "DataName") or "")
        sanma = str(_field(md, "Sanma_Eng_Name_k__BackingField", "Sanma_Eng_Name") or "")
        sub_name = _field(md, "SubName_k__BackingField", "SubName")
        show_unit = bool(_field(md, "ShowUnit_k__BackingField", "ShowUnit", default=False))
    return DataWidget(
        **common_kwargs,
        data_name=data_name,
        sanma_eng_name=sanma,
        sub_name=sub_name,
        show_unit=show_unit,
        font=font,
    )


def _parse_widget(item) -> Optional[WidgetDef]:
    cls = item.__class__.__name__
    try:
        if cls == "UsbMonitorL_GraphImage":
            return _parse_image_widget(item)
        if cls == "UsbMonitorL_GraphLine":
            return _parse_chart_widget(item)
        if cls.startswith("UsbMonitorL_GraphItem"):
            return _parse_data_or_text_widget(item)
        log.warning("Unknown widget class: %s", cls)
        return None
    except Exception as exc:
        log.exception("Failed to parse widget of class %s: %s", cls, exc)
        return None


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def parse_turtheme(path) -> ThemeDef:
    """Parse a .turtheme file and return a ThemeDef.

    `path` can be a str or pathlib.Path.
    """
    p = Path(path)
    with open(p, "rb") as f:
        root = nrbf.read_stream(f)

    front_color = Color.from_system_drawing(_field(root, "frontColor_k__BackingField"))
    back_color = Color.from_system_drawing(_field(root, "backColor_k__BackingField"))

    gl = _field(root, "GraphList_k__BackingField")
    items: Sequence = []
    if gl is not None:
        items = _field(gl, "Collection_1_items", default=[]) or []

    widgets: List[WidgetDef] = []
    for item in items:
        w = _parse_widget(item)
        if w is not None:
            widgets.append(w)

    return ThemeDef(
        name=str(_field(root, "name_k__BackingField") or ""),
        width=int(_field(root, "width_k__BackingField", default=0) or 0),
        height=int(_field(root, "height_k__BackingField", default=0) or 0),
        is_landscape=bool(_field(root, "isLanscape_k__BackingField", default=False)),  # original typo
        front_color=front_color,
        back_color=back_color,
        video_path_local=_field(root, "videoPath_k__BackingField") or None,
        video_target_path=_field(root, "videoTargetPath_k__BackingField") or None,
        video_name=_field(root, "videoName_k__BackingField") or None,
        is_visual_theme=bool(_field(root, "isVisualTheme_k__BackingField", default=False)),
        is_temp_theme=bool(_field(root, "isTempTheme_k__BackingField", default=False)),
        is_aida_theme=bool(_field(root, "isAidaTheme_k__BackingField", default=False)),
        widgets=widgets,
        raw_root=root,
    )
