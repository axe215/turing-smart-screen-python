"""ThemeManager — owns the active ThemeEngine, knows the catalog of
themes available in res/themes/, and can swap which one is running.

Used by the Phase 5c Flask UI and by any future tray/CLI front-end.
The manager itself is UI-agnostic — it just exposes start/stop/swap +
a status snapshot, and a list_themes() catalog.

Memory audit (Phase 5f):

Per-theme state lives in ThemeEngine → WidgetRenderer (font_cache,
image_cache, chart_history) and StreamingThread (reads chunks and
discards them every iteration). All caches are bounded:
  - font_cache: ≤ ~30 entries (one per (family, scaled_size, bold))
  - image_cache: ≤ # of image widgets in the theme
  - chart_history: explicit deque(maxlen=…)

On theme swap, _stop_locked() drops the engine reference; Python's
refcount GC reclaims the old objects since there are no cycles. We
follow up with gc.collect() to make any remaining cycles (e.g. via
threading.Thread back-references) release immediately rather than
waiting for the next generational sweep.

Long-lived shared state:
  - The LcdCommTuringUSB handle stays alive for the manager's lifetime
    (passed in once at construction). Engines reuse it.
  - LHM .NET runtime initialized once at first sensor read, kept for
    the process lifetime by design — not a leak.

Memory of the running process is exposed via status()["process_rss_mb"]
so users can spot any drift in the UI footer over hours.
"""
from __future__ import annotations

import gc
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .engine import ThemeEngine
from .runtime import build_runtime, load_theme
from .upstream_adapter import upstream_to_axe215_dict

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
    # Background classification — used by the UI filter and badge:
    #   "video"   .mp4 or .webm
    #   "gif"     animated .gif
    #   "image"   static .png / .jpg / .jpeg
    #   "none"    no background asset (rare)
    background_type: str = "none"
    background_path: Optional[Path] = None  # absolute path to the asset
    # Schema tag — "axe215_v1" → runs in our engine; "upstream" → mathoudebine
    # main.py-only (we can't activate it from the manager).
    schema: str = "axe215_v1"

    @property
    def runnable(self) -> bool:
        # Both schemas can now run: native axe215_v1 goes straight into
        # the engine, upstream themes are adapted on the fly into the
        # same runtime structure (see upstream_adapter.py).
        return self.schema in ("axe215_v1", "upstream")

    def to_dict(self) -> Dict[str, Any]:
        # Always offer a /preview URL: the endpoint generates the
        # preview lazily (first MP4 frame, first GIF frame, or the
        # source image) and returns 404 only if nothing is available.
        preview_url = None
        if self.preview_path is not None or self.background_type in ("video", "gif", "image"):
            preview_url = f"/themes/{self.dir_name}/preview"
        return {
            "name": self.name,
            "dir_name": self.dir_name,
            "canvas_width": self.canvas[0],
            "canvas_height": self.canvas[1],
            "widget_count": self.widget_count,
            "has_video": self.has_video,
            "video_name": self.video_name,
            "preview_url": preview_url,
            "background_type": self.background_type,
            "schema": self.schema,
            "runnable": self.runnable,
        }


# Upstream "DISPLAY_SIZE" → (landscape_width, landscape_height). Used as a
# fallback when the theme doesn't carry an explicit BACKGROUND with WIDTH/HEIGHT.
_UPSTREAM_DISPLAY_SIZE_MAP = {
    "0.96": (160, 80),
    "2.1": (480, 480),
    "2.8": (480, 480),
    "3.5": (480, 320),
    "4.6": (960, 320),
    "5": (800, 480),
    "5.2": (1280, 720),
    "8": (1920, 480),     # eva.rei-style 8" landscape
    "8.8": (1920, 480),
    "9.2": (1920, 462),
    "12.3": (1920, 720),
}


def _classify_background(path: Path) -> str:
    """Map a file extension to one of our background_type tags."""
    ext = path.suffix.lower()
    if ext in (".mp4", ".webm", ".mov"):
        return "video"
    if ext == ".gif":
        return "gif"
    if ext in (".png", ".jpg", ".jpeg", ".bmp"):
        return "image"
    return "none"


def _find_preview(entry: Path) -> Optional[Path]:
    """Look for a hand-supplied preview.{png,jpg,jpeg} alongside theme.yaml."""
    for cand in ("preview.png", "preview.jpg", "preview.jpeg"):
        p = entry / cand
        if p.exists():
            return p
    return None


def _count_stats_leaves(stats) -> int:
    """Walk upstream STATS hierarchy and count TEXT / GRAPH / RADIAL leaves.

    Structure is roughly STATS.<METRIC>.<SUBKIND>.<TEXT|GRAPH|RADIAL> with
    arbitrary nesting. We count any dict that has a SHOW key OR a TEXT/X/Y
    triplet that looks like a renderable widget.
    """
    if not isinstance(stats, dict):
        return 0
    count = 0
    for k, v in stats.items():
        if not isinstance(v, dict):
            continue
        # A leaf has X+Y (drawable position) or SHOW (toggle)
        if ("X" in v and "Y" in v) or "SHOW" in v:
            count += 1
        count += _count_stats_leaves(v)
    return count


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
    # Override theme font colors with black + white stroke for max
    # legibility over light video frames. Off by default — themes
    # designed with specific colors (most upstream themes) deserve
    # to render in those colors.
    force_black_text: bool = False
    # Backlight brightness, 0..100. Applied to the LCD on theme start
    # and on-demand via /api/brightness for live adjustment.
    brightness: int = 50

    def as_kwargs(self) -> Dict[str, Any]:
        return {
            "rotate_180": self.rotate_180,
            "rotate_video": self.rotate_video,
            "font_scale": self.font_scale,
            "screen": self.screen,
            "force_black_text": self.force_black_text,
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
        """Scan themes_dir for subfolders that contain a theme.yaml.

        Both our axe215_v1 schema and upstream (mathoudebine) themes
        are parsed and returned. The runnable ones (our schema) come
        first; upstream themes are kept for browsing/preview (with
        runnable=False so the UI can disable the Activate button).
        """
        out_native: List[ThemeInfo] = []
        out_upstream: List[ThemeInfo] = []
        bad: List[str] = []
        if not self.themes_dir.exists():
            log.warning("themes_dir %s does not exist", self.themes_dir)
            return []
        for entry in sorted(self.themes_dir.iterdir()):
            if not entry.is_dir():
                continue
            yaml_path = entry / "theme.yaml"
            if not yaml_path.exists():
                continue
            try:
                info = self._load_theme_info(entry, yaml_path)
            except Exception as exc:
                log.warning("failed to read %s: %s", yaml_path, exc)
                bad.append(entry.name)
                continue
            if info is None:
                bad.append(entry.name)
            elif info.schema == "axe215_v1":
                out_native.append(info)
            else:
                out_upstream.append(info)
        if bad:
            log.info("skipped %d unreadable theme dirs: %s",
                     len(bad), ", ".join(bad[:5]) + ("…" if len(bad) > 5 else ""))
        return out_native + out_upstream

    def get_theme(self, dir_name: str) -> Optional[ThemeInfo]:
        for t in self.list_themes():
            if t.dir_name == dir_name:
                return t
        return None

    def _load_theme_info(self, entry: Path, yaml_path: Path) -> Optional[ThemeInfo]:
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # Dispatch on schema marker — our emitter writes schema_version: 1;
        # upstream themes don't.
        if data.get("schema_version") == 1:
            return self._parse_axe215_theme(entry, yaml_path, data)
        # Anything else with a `display:` block is upstream mathoudebine.
        if isinstance(data.get("display"), dict):
            return self._parse_upstream_theme(entry, yaml_path, data)
        return None

    # ---- axe215_v1 parser ---------------------------------------------

    def _parse_axe215_theme(self, entry, yaml_path, data) -> ThemeInfo:
        canvas = data.get("canvas") or {}
        widgets = data.get("widgets") or []
        video = data.get("video") or {}
        image_block = data.get("image") or {}
        background_type = "none"
        background_path = None
        video_name = None
        if video.get("path"):
            video_abs = (entry / video["path"]).resolve()
            background_type = _classify_background(video_abs) or "video"
            background_path = video_abs
            video_name = video.get("path")
        elif image_block.get("path"):
            img_abs = (entry / image_block["path"]).resolve()
            background_type = _classify_background(img_abs) or "image"
            background_path = img_abs
        preview = _find_preview(entry)
        return ThemeInfo(
            name=str(data.get("name", entry.name)),
            dir_name=entry.name,
            yaml_path=yaml_path,
            canvas=(int(canvas.get("width", 1920)), int(canvas.get("height", 480))),
            widget_count=len(widgets),
            has_video=(background_type == "video"),
            video_name=video_name,
            preview_path=preview,
            background_type=background_type,
            background_path=background_path,
            schema="axe215_v1",
        )

    # ---- Upstream (mathoudebine) parser -------------------------------

    def _parse_upstream_theme(self, entry, yaml_path, data) -> ThemeInfo:
        """Parse a mathoudebine YAML theme just enough for the catalog UI.

        Counts widgets (static_text + static_images-excluding-BACKGROUND +
        the leaf TEXT/GRAPH entries under STATS), resolves canvas size from
        the BACKGROUND image if present (most reliable), or falls back to
        DISPLAY_SIZE → known dimensions, with orientation honored.
        """
        display = data.get("display") or {}
        static_text = data.get("static_text") or {}
        static_images = data.get("static_images") or {}
        stats = data.get("STATS") or {}

        # Canvas: prefer BACKGROUND.WIDTH/HEIGHT, else DISPLAY_SIZE lookup.
        canvas_w, canvas_h = (0, 0)
        background_widget = static_images.get("BACKGROUND") if isinstance(static_images, dict) else None
        if isinstance(background_widget, dict):
            try:
                canvas_w = int(background_widget.get("WIDTH", 0))
                canvas_h = int(background_widget.get("HEIGHT", 0))
            except (TypeError, ValueError):
                pass
        if canvas_w == 0 or canvas_h == 0:
            size_key = str(display.get("DISPLAY_SIZE", "")).strip().strip('"').rstrip('"')
            w, h = _UPSTREAM_DISPLAY_SIZE_MAP.get(size_key, (1920, 480))
            orient = str(display.get("DISPLAY_ORIENTATION", "landscape")).lower()
            if orient.startswith("portrait"):
                w, h = h, w
            canvas_w, canvas_h = w, h

        # Widget count: static_text + static_images (minus BACKGROUND) + STATS leaves.
        widget_count = 0
        if isinstance(static_text, dict):
            widget_count += len(static_text)
        if isinstance(static_images, dict):
            widget_count += sum(1 for k in static_images if k != "BACKGROUND")
        widget_count += _count_stats_leaves(stats)

        # Background classification — most upstream themes use a static PNG.
        bg_path = None
        bg_type = "none"
        if isinstance(background_widget, dict):
            rel = background_widget.get("PATH")
            if rel:
                p = (entry / rel).resolve()
                if p.exists():
                    bg_path = p
                    bg_type = _classify_background(p)
                else:
                    bg_type = _classify_background(Path(rel))

        return ThemeInfo(
            name=str(data.get("author") or entry.name),
            dir_name=entry.name,
            yaml_path=yaml_path,
            canvas=(canvas_w, canvas_h),
            widget_count=widget_count,
            has_video=(bg_type == "video"),
            video_name=None,
            preview_path=_find_preview(entry) or bg_path,
            background_type=bg_type,
            background_path=bg_path,
            schema="upstream",
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, dir_name: str, params: Optional[EngineParams] = None) -> None:
        """Stop the current engine (if any) and start the named theme."""
        info = self.get_theme(dir_name)
        if info is None:
            raise ValueError(f"theme '{dir_name}' not found in {self.themes_dir}")
        if not info.runnable:
            raise ValueError(
                f"theme '{dir_name}' has an unsupported schema ({info.schema})"
            )

        if params is not None:
            self.params = params

        with self._lock:
            self._stop_locked()

            if info.schema == "axe215_v1":
                theme_runtime = load_theme(info.yaml_path)
            else:
                # Upstream mathoudebine theme — adapt to our runtime
                # in memory. Static-image background, widgets translated
                # from static_text + STATS hierarchy.
                with open(info.yaml_path, "r", encoding="utf-8") as f:
                    upstream_data = yaml.safe_load(f) or {}
                converted = upstream_to_axe215_dict(
                    upstream_data,
                    theme_dir=info.yaml_path.parent,
                )
                log.info(
                    "ThemeManager: adapted upstream %s → %d widgets",
                    info.dir_name, len(converted.get("widgets", [])),
                )
                theme_runtime = build_runtime(
                    converted,
                    theme_dir=info.yaml_path.parent,
                    default_name=info.dir_name,
                )

            engine = ThemeEngine(
                theme_runtime,
                self.lcd,
                **self.params.as_kwargs(),
            )
            engine.start(widget_period=self.params.widget_period)
            self.active_engine = engine
            self.active_theme = info.dir_name
            # Apply brightness once the screen is alive (calling earlier
            # races against the LCD's own init in some firmware revs).
            try:
                self.lcd.SetBrightness(int(self.params.brightness))
            except Exception as exc:
                log.warning("SetBrightness(%s) failed: %s", self.params.brightness, exc)
            log.info("ThemeManager.start: active=%s schema=%s", info.dir_name, info.schema)

    def set_brightness(self, level: int) -> None:
        """Apply brightness immediately and remember it for next start."""
        level = max(0, min(100, int(level)))
        with self._lock:
            self.params.brightness = level
        try:
            self.lcd.SetBrightness(level)
        except Exception as exc:
            log.warning("SetBrightness(%s) failed: %s", level, exc)
            raise

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
        # Explicit cache clear on the old renderer — drops font/image/
        # chart_history immediately instead of waiting for GC to find it.
        try:
            renderer = getattr(self.active_engine, "renderer", None)
            if renderer is not None:
                renderer.clear_caches()
        except Exception as exc:
            log.debug("renderer.clear_caches() raised: %s", exc)
        self.active_engine = None
        self.active_theme = None
        # Belt-and-braces: trigger a collection so the now-orphaned Pillow
        # bitmaps return their C-extension memory to the OS promptly.
        gc.collect()

    def swap(self, dir_name: str, params: Optional[EngineParams] = None) -> None:
        """Alias for start() — kept for readability at call-sites."""
        self.start(dir_name, params=params)

    def replace_live(self, dir_name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Hot-reload the active engine's widget set from an in-memory
        theme dict (i.e. the editor's pre-Save state). Returns:

          {"ok": True}                          on success
          {"ok": False, "reason": "<msg>"}      on rejection (theme not
                                                active, canvas mismatch,
                                                bg-type mismatch, …)

        Does NOT mutate the on-disk YAML — that's a separate Save step.
        """
        from .runtime import build_runtime
        with self._lock:
            if self.active_engine is None or self.active_theme != dir_name:
                return {"ok": False, "reason": "this theme is not active — Activate it first"}
            info = self.get_theme(dir_name)
            if info is None:
                return {"ok": False, "reason": "theme not found"}
            try:
                new_runtime = build_runtime(data, info.yaml_path.parent, default_name=dir_name)
            except Exception as exc:
                log.exception("replace_live: build_runtime failed")
                return {"ok": False, "reason": f"theme data invalid: {exc}"}
            return self.active_engine.replace_runtime(new_runtime)

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
                    "force_black_text": self.params.force_black_text,
                    "brightness": self.params.brightness,
                },
                "engine": engine_status,
                "themes_dir": str(self.themes_dir),
                "process_rss_mb": _process_rss_mb(),
                "ts": time.time(),
            }


def _process_rss_mb() -> Optional[float]:
    """Resident-set size of the current process in MB. Used by the UI
    footer to spot slow leaks. Returns None if psutil is unavailable
    (importing this module on a host without psutil is fine)."""
    try:
        import psutil  # local import: optional
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        return None
