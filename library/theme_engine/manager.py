"""ThemeManager — owns the active ThemeEngine, knows the catalog of
themes available in res/themes/, and can swap which one is running.

Used by the Phase 5c Flask UI and by any future tray/CLI front-end.
The manager itself is UI-agnostic — it just exposes start/stop/swap +
a status snapshot, and a list_themes() catalog.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .engine import ThemeEngine
from .runtime import load_theme

log = logging.getLogger(__name__)


@dataclass
class ThemeInfo:
    """Lightweight directory-listing entry for a single theme."""
    name: str                       # display name (theme.yaml `name` or dir name)
    dir_name: str                   # folder name under res/themes/
    yaml_path: Path
    canvas: tuple                   # (width, height) in design coords
    widget_count: int
    has_video: bool
    video_name: Optional[str] = None
    preview_path: Optional[Path] = None  # if a preview.png/jpg sits next to theme.yaml

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "dir_name": self.dir_name,
            "canvas_width": self.canvas[0],
            "canvas_height": self.canvas[1],
            "widget_count": self.widget_count,
            "has_video": self.has_video,
            "video_name": self.video_name,
            "preview_url": f"/themes/{self.dir_name}/preview" if self.preview_path else None,
        }


@dataclass
class EngineParams:
    """Snapshot of params used to instantiate ThemeEngine. Persisted by
    the manager so a UI can pre-fill controls with the last-used values
    and the manager can re-instantiate on theme swap."""
    rotate_180: bool = True              # most users mount the screen flipped
    rotate_video: int = 180              # ↑ matches: pre-rotate MP4 once on first run
    font_scale: float = 1.3
    widget_period: float = 1.0
    screen: str = "9.2"

    def as_kwargs(self) -> Dict[str, Any]:
        return {
            "rotate_180": self.rotate_180,
            "rotate_video": self.rotate_video,
            "font_scale": self.font_scale,
            "screen": self.screen,
        }


class ThemeManager:
    def __init__(self, themes_dir: Path, lcd):
        self.themes_dir = Path(themes_dir).resolve()
        self.lcd = lcd
        self._lock = threading.Lock()
        self.active_engine: Optional[ThemeEngine] = None
        self.active_theme: Optional[str] = None  # dir_name
        self.params = EngineParams()

    # ------------------------------------------------------------------
    # Catalog
    # ------------------------------------------------------------------

    def list_themes(self) -> List[ThemeInfo]:
        """Scan themes_dir for subfolders that contain a theme.yaml."""
        out: List[ThemeInfo] = []
        if not self.themes_dir.exists():
            log.warning("themes_dir %s does not exist", self.themes_dir)
            return out
        for entry in sorted(self.themes_dir.iterdir()):
            if not entry.is_dir():
                continue
            yaml_path = entry / "theme.yaml"
            if not yaml_path.exists():
                continue
            try:
                info = self._load_theme_info(entry, yaml_path)
                if info is not None:
                    out.append(info)
            except Exception as exc:
                log.warning("failed to read %s: %s", yaml_path, exc)
        return out

    def get_theme(self, dir_name: str) -> Optional[ThemeInfo]:
        for t in self.list_themes():
            if t.dir_name == dir_name:
                return t
        return None

    def _load_theme_info(self, entry: Path, yaml_path: Path) -> Optional[ThemeInfo]:
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        canvas = data.get("canvas") or {}
        widgets = data.get("widgets") or []
        video = data.get("video") or {}
        # Look for a hand-supplied preview image
        preview = None
        for cand in ("preview.png", "preview.jpg", "preview.jpeg"):
            p = entry / cand
            if p.exists():
                preview = p
                break
        return ThemeInfo(
            name=str(data.get("name", entry.name)),
            dir_name=entry.name,
            yaml_path=yaml_path,
            canvas=(int(canvas.get("width", 1920)), int(canvas.get("height", 480))),
            widget_count=len(widgets),
            has_video=bool(video.get("path")),
            video_name=video.get("path"),
            preview_path=preview,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, dir_name: str, params: Optional[EngineParams] = None) -> None:
        """Stop the current engine (if any) and start the named theme."""
        info = self.get_theme(dir_name)
        if info is None:
            raise ValueError(f"theme '{dir_name}' not found in {self.themes_dir}")

        if params is not None:
            self.params = params

        with self._lock:
            # Stop any active engine first. ThemeEngine.stop() is idempotent
            # and joins both threads, so we can safely build a new engine.
            self._stop_locked()

            theme_runtime = load_theme(info.yaml_path)
            engine = ThemeEngine(
                theme_runtime,
                self.lcd,
                **self.params.as_kwargs(),
            )
            engine.start(widget_period=self.params.widget_period)
            self.active_engine = engine
            self.active_theme = info.dir_name
            log.info("ThemeManager.start: active=%s", info.dir_name)

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self.active_engine is None:
            return
        try:
            self.active_engine.stop()
        except Exception as exc:
            log.warning("active engine stop raised: %s", exc)
        self.active_engine = None
        self.active_theme = None

    def swap(self, dir_name: str, params: Optional[EngineParams] = None) -> None:
        """Alias for start() — kept for readability at call-sites."""
        self.start(dir_name, params=params)

    # ------------------------------------------------------------------
    # Status snapshot for UIs
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        with self._lock:
            running = self.active_engine is not None and self.active_engine.is_running()
            engine_status = self.active_engine.status() if running else None
            return {
                "active_theme": self.active_theme,
                "running": running,
                "params": {
                    "rotate_180": self.params.rotate_180,
                    "rotate_video": self.params.rotate_video,
                    "font_scale": self.params.font_scale,
                    "widget_period": self.params.widget_period,
                    "screen": self.params.screen,
                },
                "engine": engine_status,
                "themes_dir": str(self.themes_dir),
                "ts": time.time(),
            }
