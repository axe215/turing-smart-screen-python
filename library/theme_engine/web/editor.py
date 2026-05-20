"""Editor endpoints — load / save / preview themes from the dashboard.

The editor lives at `/editor/<dir_name>` and is a separate single-page
view served on top of the existing dashboard. All I/O is JSON:

  GET  /api/themes/<dir>           → full YAML as JSON, plus runtime
                                     hints (canvas size, schema, asset
                                     URL for the background)
  POST /api/themes/<dir>/save      → JSON body becomes the new YAML.
                                     Old file is rotated to .bak first.
  POST /api/themes/<dir>/preview   → render the supplied JSON to a PNG
                                     at design-canvas dimensions (no
                                     screen rotation/crop). Returned as
                                     image/png inline; caller is
                                     responsible for cache-busting via
                                     a query-string nonce.

A few hard rules:

* No edits to upstream-schema themes go through this module — Phase 6d
  clones them to axe215_v1 first. Until then, /editor/<dir> on an
  upstream theme returns 409.
* Save writes an `editor_version` marker into the YAML so we can tell
  later which YAMLs were touched by the GUI vs the .turtheme parser.
"""
from __future__ import annotations

import io
import logging
import os
import platform
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from flask import abort, jsonify, render_template, request, send_file

from ..data_sources import DataSourceRegistry, DEFAULT_SOURCES
from ..renderer import WidgetRenderer
from ..runtime import build_runtime

log = logging.getLogger(__name__)


# Process-wide font cache. Building it scans every .ttf/.otf in the
# themes/upstream/Windows fonts roots and reads the name table via
# Pillow — expensive enough (a few seconds on Win11) that we want to
# pay it once. Invalidated only on process restart.
_FONT_CACHE: Optional[List[Dict[str, str]]] = None


def _font_roots(themes_dir: Path) -> List[Tuple[str, Path]]:
    """Return ordered (origin_label, path) roots to scan for fonts.

    Themes take precedence over the upstream bundle, which takes
    precedence over the system fonts directory. The label is shown in
    the UI so the user knows where a font comes from (some Windows-only
    fonts won't survive moving the theme to another PC).
    """
    repo_root = themes_dir.parent.parent  # res/themes → repo/
    roots: List[Tuple[str, Path]] = []
    # Per-theme fonts live under each theme; the dashboard's font picker
    # collapses them into one global list — picking one and saving it as
    # `family: <name>` works as long as the theme also bundles the file
    # (Phase 6e's required_fonts list will surface this).
    for theme in sorted(themes_dir.iterdir()):
        if theme.is_dir() and (theme / "fonts").is_dir():
            roots.append((f"theme/{theme.name}", theme / "fonts"))
    upstream = repo_root / "res" / "fonts"
    if upstream.is_dir():
        roots.append(("upstream", upstream))
    if platform.system() == "Windows":
        win = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        if win.is_dir():
            roots.append(("system", win))
    return roots


def _scan_fonts(themes_dir: Path) -> List[Dict[str, str]]:
    """Walk every font root and read its TTF/OTF name tables. The
    returned list is what the editor's font picker consumes.

    Each item: {family, origin, file}. Duplicates by family name are
    collapsed (theme-bundled wins over upstream wins over system).
    """
    global _FONT_CACHE
    if _FONT_CACHE is not None:
        return _FONT_CACHE
    from PIL import ImageFont
    by_family: Dict[str, Dict[str, str]] = {}
    for origin, root in _font_roots(themes_dir):
        try:
            for f in root.rglob("*"):
                if not f.is_file():
                    continue
                ext = f.suffix.lower()
                if ext not in (".ttf", ".otf", ".ttc"):
                    continue
                try:
                    ft = ImageFont.truetype(str(f), 12)
                    family, _style = ft.getname()
                except Exception:
                    continue
                if not family or family in by_family:
                    continue
                by_family[family] = {
                    "family": family,
                    "origin": origin,
                    "file": str(f),
                }
        except OSError as exc:
            log.debug("font root %s scan failed: %s", root, exc)
    out = sorted(by_family.values(), key=lambda x: x["family"].lower())
    _FONT_CACHE = out
    log.info("font picker indexed %d families", len(out))
    return out


def _read_yaml(yaml_path: Path) -> Dict[str, Any]:
    with open(yaml_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _atomic_write_yaml(yaml_path: Path, data: Dict[str, Any]) -> None:
    """Write `data` to yaml_path, keeping a timestamped .bak of the
    previous content so the user can recover from a bad edit by
    renaming the file back."""
    yaml_path = Path(yaml_path)
    if yaml_path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = yaml_path.with_suffix(yaml_path.suffix + f".{stamp}.bak")
        try:
            shutil.copy2(yaml_path, bak)
        except OSError as exc:
            log.warning("could not back up %s: %s", yaml_path, exc)
    tmp = yaml_path.with_suffix(yaml_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    tmp.replace(yaml_path)


def _render_design_png(theme_dir: Path, data: Dict[str, Any]) -> bytes:
    """Render the in-memory theme to a PNG sized at the DESIGN canvas
    (the editor wants to see the landscape layout, not the rotated /
    cropped device-native output)."""
    from PIL import Image

    runtime = build_runtime(data, theme_dir, default_name=theme_dir.name)
    # Load background bitmap if the theme has one. Video themes show a
    # transparent overlay in the editor — the design surface stays
    # checkerboard-like so widgets are visible.
    bg = None
    if runtime.image is not None:
        bg_path = runtime.background_image_path
        if bg_path and bg_path.exists():
            try:
                bg = Image.open(bg_path)
            except Exception:
                bg = None
    elif runtime.video is not None:
        # First-frame extraction for video themes — reuses the same
        # preview cache the dashboard uses.
        from ..preview import resolve_preview
        cached = resolve_preview(theme_dir, "video", runtime.video_path)
        if cached and cached.exists():
            try:
                bg = Image.open(cached)
            except Exception:
                bg = None

    # Render at design canvas dimensions. screen='9.2' is the rotation
    # target but we override by NOT calling render_frame's rotate path
    # — see below.
    renderer = WidgetRenderer(
        runtime,
        DataSourceRegistry(),
        screen="9.2",
        font_scale=1.0,
        background_image=bg,
        force_black_text=False,
    )
    # Mimic the early portion of render_frame() but emit the landscape
    # canvas instead of the rotated portrait output the screen wants.
    if renderer.background_image is not None:
        canvas = renderer.background_image.copy()
    else:
        canvas = Image.new(
            "RGBA",
            (renderer.design_w, renderer.design_h),
            (32, 32, 32, 255),  # opaque dark grey so transparent widgets show
        )
    from PIL import ImageDraw
    draw = ImageDraw.Draw(canvas)
    for w in runtime.widgets:
        if w.hide or not w.enabled:
            continue
        try:
            renderer._render_widget(canvas, draw, w)
        except Exception as exc:
            log.warning("editor preview: widget %s failed: %s", w.id, exc)

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG", optimize=False)
    renderer.clear_caches()
    return buf.getvalue()


def register_editor_routes(app, manager) -> None:
    """Attach editor endpoints to the existing Flask app."""

    @app.route("/editor/<dir_name>")
    def editor_page(dir_name: str):
        info = manager.get_theme(dir_name)
        if info is None:
            abort(404)
        if info.schema != "axe215_v1":
            # Until Phase 6d, upstream themes are not editable in place.
            # We surface a friendly hint instead of a generic 409.
            return render_template(
                "editor_unavailable.html",
                dir_name=dir_name,
                schema=info.schema,
            ), 409
        return render_template("editor.html", dir_name=dir_name)

    @app.route("/api/themes/<dir_name>", methods=["GET"])
    def api_theme_get(dir_name: str):
        info = manager.get_theme(dir_name)
        if info is None:
            abort(404)
        if info.schema != "axe215_v1":
            return jsonify({"error": "upstream themes are not editable yet (Phase 6d)"}), 409
        try:
            data = _read_yaml(info.yaml_path)
        except Exception as exc:
            log.exception("failed to read %s", info.yaml_path)
            return jsonify({"error": str(exc)}), 500
        return jsonify({
            "dir_name": dir_name,
            "schema": info.schema,
            "data": data,
            "preview_url": f"/api/themes/{dir_name}/preview?t={int(time.time())}",
            "sources": sorted(DEFAULT_SOURCES.keys()),
        })

    @app.route("/api/themes/<dir_name>/save", methods=["POST"])
    def api_theme_save(dir_name: str):
        info = manager.get_theme(dir_name)
        if info is None:
            abort(404)
        if info.schema != "axe215_v1":
            return jsonify({"error": "upstream themes are not editable yet (Phase 6d)"}), 409
        body = request.get_json(silent=True) or {}
        data = body.get("data")
        if not isinstance(data, dict):
            return jsonify({"error": "body.data must be an object"}), 400
        # Stamp the file so we know it went through the editor.
        data["editor_version"] = 1
        data["editor_last_saved"] = datetime.now().isoformat(timespec="seconds")
        try:
            _atomic_write_yaml(info.yaml_path, data)
        except Exception as exc:
            log.exception("save failed for %s", info.yaml_path)
            return jsonify({"error": str(exc)}), 500
        return jsonify({
            "ok": True,
            "preview_url": f"/api/themes/{dir_name}/preview?t={int(time.time())}",
        })

    @app.route("/api/themes/<dir_name>/preview", methods=["GET", "POST"])
    def api_theme_design_preview(dir_name: str):
        """Render the theme at design dimensions.

        GET  → uses the YAML currently on disk (cheap path for the
               editor's initial paint).
        POST → uses the JSON body's `data` (for in-flight edits before
               the user has hit Save).
        """
        info = manager.get_theme(dir_name)
        if info is None:
            abort(404)
        if info.schema != "axe215_v1":
            return jsonify({"error": "upstream themes are not previewable yet (Phase 6d)"}), 409
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            data = body.get("data")
            if not isinstance(data, dict):
                return jsonify({"error": "body.data must be an object"}), 400
        else:
            try:
                data = _read_yaml(info.yaml_path)
            except Exception as exc:
                return jsonify({"error": str(exc)}), 500
        try:
            png = _render_design_png(info.yaml_path.parent, data)
        except Exception as exc:
            log.exception("design render failed for %s", dir_name)
            return jsonify({"error": str(exc)}), 500
        return send_file(
            io.BytesIO(png),
            mimetype="image/png",
            max_age=0,  # editor previews are always fresh; cache-busted by ?t=
        )

    @app.route("/api/fonts")
    def api_fonts():
        """Return all available fonts grouped by family. Cached for
        the lifetime of the process — restart phase5_manager.py to pick
        up newly installed fonts."""
        items = _scan_fonts(manager.themes_dir)
        return jsonify({"fonts": items, "count": len(items)})

    @app.route("/api/themes/<dir_name>/asset/<path:asset>")
    def api_theme_asset(dir_name: str, asset: str):
        """Serve raw asset files from a theme directory (background
        image, video frame, etc.) so the editor's canvas can <img>
        them directly without going through the preview cache."""
        info = manager.get_theme(dir_name)
        if info is None:
            abort(404)
        # Defensive: no parent-dir escapes.
        safe = (info.yaml_path.parent / asset).resolve()
        try:
            safe.relative_to(info.yaml_path.parent.resolve())
        except ValueError:
            abort(403)
        if not safe.exists() or not safe.is_file():
            abort(404)
        return send_file(str(safe), max_age=3600, conditional=True)
