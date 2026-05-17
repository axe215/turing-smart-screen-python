"""turtheme — parser for UsbMonitorL .turtheme files (.NET BinaryFormatter).

`parse_turtheme(path)` returns a `ThemeDef` with all widget positions,
fonts, colors, and embedded image bytes.

Backed by `nrbf.py` (vendored from gurnec/Undo_FFG, BSD-2).
"""
from .parser import (
    parse_turtheme,
    ThemeDef,
    WidgetDef,
    DataWidget,
    TextWidget,
    ChartWidget,
    ImageWidget,
    FontConfig,
    Color,
    Alignment,
)

__all__ = [
    "parse_turtheme",
    "ThemeDef",
    "WidgetDef",
    "DataWidget",
    "TextWidget",
    "ChartWidget",
    "ImageWidget",
    "FontConfig",
    "Color",
    "Alignment",
]
