"""theme_engine — runs YAML themes (our schema) on Turing 9.2" with video + widgets.

High-level:

    from library.theme_engine import ThemeEngine, load_theme

    theme = load_theme("res/themes/eva.rei/theme.yaml")
    engine = ThemeEngine(theme, lcd)
    engine.run(duration=None, widget_period=1.0)

The engine starts a background StreamingThread that loops the theme's
video, and in the main thread it renders widgets (CPU/GPU/RAM/clock) to
a full-screen RGBA PNG and pushes via cmd 102 every `widget_period`.
"""
# Lightweight re-exports — modules that don't pull in usb/psutil etc
from .runtime import ThemeRuntime, load_theme, WidgetSpec, FontSpec
from .renderer import WidgetRenderer

# `data_sources`, `streaming`, and `engine` have heavier deps (psutil, pyusb).
# Expose them via lazy __getattr__ so that `import library.theme_engine`
# from a dry-run / static analysis path doesn't pull them in.


def __getattr__(name):  # PEP 562
    if name == "DataSourceRegistry":
        from .data_sources import DataSourceRegistry
        return DataSourceRegistry
    if name == "DEFAULT_SOURCES":
        from .data_sources import DEFAULT_SOURCES
        return DEFAULT_SOURCES
    if name == "StreamingThread":
        from .streaming import StreamingThread
        return StreamingThread
    if name == "ThemeEngine":
        from .engine import ThemeEngine
        return ThemeEngine
    raise AttributeError(f"module {__name__} has no attribute {name}")


__all__ = [
    "ThemeRuntime",
    "load_theme",
    "WidgetSpec",
    "FontSpec",
    "WidgetRenderer",
    "DataSourceRegistry",
    "DEFAULT_SOURCES",
    "StreamingThread",
    "ThemeEngine",
]
