"""Load a theme.yaml (our schema v1) into a strongly-typed ThemeRuntime."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

log = logging.getLogger(__name__)


@dataclass
class FontSpec:
    family: str = ""
    size: int = 12
    bold: bool = False
    color: Tuple[int, int, int, int] = (255, 255, 255, 255)
    # alignment from .turtheme: [horizontal, vertical] (0=left/top, 1=center, 2=right/bottom)
    alignment: Tuple[int, int] = (0, 0)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["FontSpec"]:
        if not d:
            return None
        color = d.get("color") or [255, 255, 255, 255]
        if len(color) == 3:
            color = list(color) + [255]
        return cls(
            family=str(d.get("family", "")),
            size=int(d.get("size", 12)),
            bold=bool(d.get("bold", False)),
            color=tuple(int(c) for c in color),
            alignment=tuple(d.get("alignment", [0, 0])),
        )


@dataclass
class WidgetSpec:
    id: str
    type: str  # "data" | "text" | "chart" | "image"
    x: int
    y: int
    raw: Dict[str, Any]  # the full raw dict from YAML — for type-specific fields
    hide: bool = False
    enabled: bool = True
    font: Optional[FontSpec] = None

    # data widgets
    source: str = ""
    legacy_source: str = ""
    show_unit: bool = False

    # text widgets
    text: str = ""

    # chart widgets
    width: int = 0
    height: int = 0
    max_value: float = 100.0
    line_color: Optional[Tuple[int, int, int, int]] = None
    fill_color: Optional[Tuple[int, int, int, int]] = None
    border_color: Optional[Tuple[int, int, int, int]] = None
    line_width: int = 1
    border_width: int = 1

    # image widgets
    image: str = ""
    scale: float = 1.0

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WidgetSpec":
        color = lambda key: tuple(d[key]) if d.get(key) else None
        ws = cls(
            id=str(d.get("id", "")),
            type=str(d.get("type", "")),
            x=int(d.get("x", 0)),
            y=int(d.get("y", 0)),
            raw=dict(d),
            hide=bool(d.get("hide", False)),
            enabled=bool(d.get("enabled", True)),
            font=FontSpec.from_dict(d.get("font")),
            source=str(d.get("source", "")),
            legacy_source=str(d.get("legacy_source", "")),
            show_unit=bool(d.get("show_unit", False)),
            text=str(d.get("text", "")),
            width=int(d.get("width", 0)),
            height=int(d.get("height", 0)),
            max_value=float(d.get("max_value", 100.0)),
            line_color=color("line_color"),
            fill_color=color("fill_color"),
            border_color=color("border_color"),
            line_width=int(d.get("line_width", 1)),
            border_width=int(d.get("border_width", 1)),
            image=str(d.get("image", "")),
            scale=float(d.get("scale", 1.0)),
        )
        return ws


@dataclass
class CanvasSpec:
    width: int
    height: int


@dataclass
class VideoSpec:
    path: str
    loop: bool = True
    framerate: int = 25


@dataclass
class ImageSpec:
    """Static background image alternative to `video`. Used by themes
    that don't have an animated source — the engine composites widgets
    onto this image and sends the result via cmd 102 (no streaming)."""
    path: str


@dataclass
class ThemeRuntime:
    """A loaded theme with paths resolved to the on-disk theme directory."""

    name: str
    canvas: CanvasSpec
    video: Optional[VideoSpec]
    image: Optional[ImageSpec]
    widgets: List[WidgetSpec]
    theme_dir: Path
    raw: Dict[str, Any]

    @property
    def video_path(self) -> Optional[Path]:
        if self.video is None:
            return None
        return (self.theme_dir / self.video.path).resolve()

    @property
    def background_image_path(self) -> Optional[Path]:
        if self.image is None:
            return None
        return (self.theme_dir / self.image.path).resolve()

    def image_path(self, rel: str) -> Path:
        return (self.theme_dir / rel).resolve()


def load_theme(yaml_path) -> ThemeRuntime:
    yaml_path = Path(yaml_path).expanduser().resolve()
    if not yaml_path.exists():
        raise FileNotFoundError(yaml_path)
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return build_runtime(data, yaml_path.parent, default_name=yaml_path.stem)


def build_runtime(data: Dict[str, Any], theme_dir: Path, default_name: str = "") -> ThemeRuntime:
    """Build a ThemeRuntime from an already-parsed YAML-like dict.

    Used by both load_theme() (reads our axe215_v1 yaml from disk) and
    the upstream adapter (constructs an equivalent dict in memory from
    a mathoudebine theme.yaml).
    """
    schema = int(data.get("schema_version", 1))
    if schema != 1:
        log.warning("theme schema_version=%d not 1; tread carefully", schema)

    canvas_d = data.get("canvas") or {}
    canvas = CanvasSpec(
        width=int(canvas_d.get("width", 1920)),
        height=int(canvas_d.get("height", 480)),
    )

    video = None
    vd = data.get("video")
    if vd:
        video = VideoSpec(
            path=str(vd.get("path", "")),
            loop=bool(vd.get("loop", True)),
            framerate=int(vd.get("framerate", 25)),
        )

    image = None
    img_data = data.get("image")
    if img_data and not video:
        image = ImageSpec(path=str(img_data.get("path", "")))

    widgets = [WidgetSpec.from_dict(d) for d in (data.get("widgets") or [])]

    return ThemeRuntime(
        name=str(data.get("name", default_name)),
        canvas=canvas,
        video=video,
        image=image,
        widgets=widgets,
        theme_dir=Path(theme_dir),
        raw=data,
    )
