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
import re
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


# Supported screens — mirror of upstream_adapter._DISPLAY_SIZE plus a
# `landscape` flag so the UI knows which orientation the manufacturer
# treats as native. Users can still pick portrait by editing canvas in
# the editor.
SUPPORTED_SCREENS: List[Dict[str, Any]] = [
    {"label": '0.96"', "size": "0.96", "width": 160,  "height": 80},
    {"label": '2.1"',  "size": "2.1",  "width": 480,  "height": 480},
    {"label": '2.8"',  "size": "2.8",  "width": 480,  "height": 480},
    {"label": '3.5"',  "size": "3.5",  "width": 480,  "height": 320},
    {"label": '4.6"',  "size": "4.6",  "width": 960,  "height": 320},
    {"label": '5"',    "size": "5",    "width": 800,  "height": 480},
    {"label": '5.2"',  "size": "5.2",  "width": 1280, "height": 720},
    {"label": '8"',    "size": "8",    "width": 1920, "height": 480},
    {"label": '8.8"',  "size": "8.8",  "width": 1920, "height": 480},
    {"label": '9.2"',  "size": "9.2",  "width": 1920, "height": 462},
    {"label": '12.3"', "size": "12.3", "width": 1920, "height": 720},
]


# Allowed image / video extensions for background uploads. Anything
# else 415s — we don't want users smuggling .exe files into a theme
# directory just because the form-data check is missing.
_ALLOWED_BG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".mp4", ".webm"}


def _safe_dir_name(name: str) -> str:
    """Turn a free-form theme name into a filesystem-safe directory
    name. Disallows separators and reserved characters; collapses
    repeated dashes/underscores."""
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", name).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"_{2,}", "_", cleaned)
    return cleaned[:64] or "theme"


def _unique_theme_dir(themes_dir: Path, base: str) -> Path:
    """Append _2, _3, … until the directory does not exist."""
    candidate = themes_dir / base
    n = 2
    while candidate.exists():
        candidate = themes_dir / f"{base}_{n}"
        n += 1
    return candidate


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

    @app.route("/api/screens")
    def api_screens():
        return jsonify({"screens": SUPPORTED_SCREENS})

    @app.route("/api/themes/new", methods=["POST"])
    def api_theme_new():
        """Create a blank axe215_v1 theme.

        Body: {name, width, height, dir_name?}
        Returns: {dir_name, editor_url}
        """
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name required"}), 400
        try:
            width = int(body.get("width"))
            height = int(body.get("height"))
        except (TypeError, ValueError):
            return jsonify({"error": "width and height required (int)"}), 400
        if width <= 0 or height <= 0 or width > 8000 or height > 8000:
            return jsonify({"error": "width/height out of range"}), 400
        base = body.get("dir_name") or _safe_dir_name(name)
        target = _unique_theme_dir(manager.themes_dir, base)
        target.mkdir(parents=True, exist_ok=False)
        # Minimal blank theme YAML
        data = {
            "schema_version": 1,
            "name": name,
            "canvas": {"width": width, "height": height},
            "widgets": [],
            "editor_version": 1,
            "editor_last_saved": datetime.now().isoformat(timespec="seconds"),
        }
        _atomic_write_yaml(target / "theme.yaml", data)
        return jsonify({
            "dir_name": target.name,
            "editor_url": f"/editor/{target.name}",
        })

    @app.route("/api/themes/<dir_name>/clone", methods=["POST"])
    def api_theme_clone(dir_name: str):
        """Clone an existing theme directory under a new name.

        Body: {new_name, dir_name?}
        Currently only clones axe215_v1 themes (Phase 6d will add the
        upstream → axe215_v1 conversion path).
        """
        info = manager.get_theme(dir_name)
        if info is None:
            abort(404)
        if info.schema != "axe215_v1":
            return jsonify({"error": "upstream theme cloning lives in Phase 6d"}), 409
        body = request.get_json(silent=True) or {}
        new_name = str(body.get("new_name") or "").strip()
        if not new_name:
            return jsonify({"error": "new_name required"}), 400
        base = body.get("dir_name") or _safe_dir_name(new_name)
        target = _unique_theme_dir(manager.themes_dir, base)
        src_dir = info.yaml_path.parent
        try:
            shutil.copytree(src_dir, target)
        except OSError as exc:
            log.exception("clone copy failed: %s → %s", src_dir, target)
            return jsonify({"error": str(exc)}), 500
        # Update the cloned theme's display name
        try:
            data = _read_yaml(target / "theme.yaml")
            data["name"] = new_name
            data["editor_version"] = 1
            data["editor_last_saved"] = datetime.now().isoformat(timespec="seconds")
            # Drop any stale .cache from the source — preview will regenerate
            cache = target / ".cache"
            if cache.exists():
                shutil.rmtree(cache, ignore_errors=True)
            _atomic_write_yaml(target / "theme.yaml", data)
        except Exception as exc:
            log.exception("clone rename failed")
            return jsonify({"error": str(exc)}), 500
        return jsonify({
            "dir_name": target.name,
            "editor_url": f"/editor/{target.name}",
        })

    @app.route("/api/themes/<dir_name>/upload", methods=["POST"])
    def api_theme_upload(dir_name: str):
        """Receive a background asset (image/video) into the theme dir.

        Saved as `_uploaded<ext>` so it doesn't collide with the active
        background.png; the crop step writes the final `background.png`
        and the editor switches the YAML's image.path to it.

        Body: multipart with field `file`.
        Returns: {filename, asset_url, width, height}
        """
        info = manager.get_theme(dir_name)
        if info is None:
            abort(404)
        if "file" not in request.files:
            return jsonify({"error": "file part missing"}), 400
        f = request.files["file"]
        if not f.filename:
            return jsonify({"error": "empty filename"}), 400
        ext = Path(f.filename).suffix.lower()
        if ext not in _ALLOWED_BG_EXTS:
            return jsonify({"error": f"extension {ext} not allowed"}), 415
        target = info.yaml_path.parent / f"_uploaded{ext}"
        try:
            f.save(str(target))
        except OSError as exc:
            log.exception("upload save failed")
            return jsonify({"error": str(exc)}), 500
        # Inspect image dimensions so the cropper knows the source size.
        meta = {"width": None, "height": None}
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"):
            try:
                from PIL import Image
                with Image.open(target) as im:
                    meta["width"], meta["height"] = im.size
            except Exception as exc:
                log.warning("upload meta probe failed: %s", exc)
        return jsonify({
            "filename": target.name,
            "asset_url": f"/api/themes/{dir_name}/asset/{target.name}",
            **meta,
        })

    @app.route("/api/themes/<dir_name>/crop-bg", methods=["POST"])
    def api_theme_crop_bg(dir_name: str):
        """Crop the uploaded asset to the requested rect and save as
        background.png in the theme directory.

        Body: {filename, crop: {x, y, w, h}, fit_canvas?: bool}
        When fit_canvas=true (default), the cropped result is scaled to
        the canvas's pixel dimensions so widget coords map 1:1.
        """
        info = manager.get_theme(dir_name)
        if info is None:
            abort(404)
        body = request.get_json(silent=True) or {}
        filename = str(body.get("filename") or "")
        crop = body.get("crop") or {}
        try:
            cx = int(crop["x"]); cy = int(crop["y"])
            cw = int(crop["w"]); ch = int(crop["h"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "crop {x,y,w,h} required (int)"}), 400
        if cw <= 0 or ch <= 0:
            return jsonify({"error": "crop w/h must be positive"}), 400
        src = (info.yaml_path.parent / filename).resolve()
        try:
            src.relative_to(info.yaml_path.parent.resolve())
        except ValueError:
            abort(403)
        if not src.exists():
            return jsonify({"error": "filename not found in theme dir"}), 404
        fit_canvas = bool(body.get("fit_canvas", True))
        try:
            from PIL import Image
            im = Image.open(src).convert("RGBA")
            box = (cx, cy, cx + cw, cy + ch)
            cropped = im.crop(box)
            if fit_canvas:
                canvas = info.canvas
                if cropped.size != canvas:
                    cropped = cropped.resize(canvas, Image.LANCZOS)
            out_path = info.yaml_path.parent / "background.png"
            cropped.save(out_path, format="PNG", optimize=True)
        except Exception as exc:
            log.exception("crop failed")
            return jsonify({"error": str(exc)}), 500
        # Update YAML to point at background.png as the image source.
        try:
            data = _read_yaml(info.yaml_path)
            data.setdefault("image", {})["path"] = "background.png"
            # Drop any conflicting video block — image takes precedence
            data.pop("video", None)
            data["editor_version"] = 1
            data["editor_last_saved"] = datetime.now().isoformat(timespec="seconds")
            _atomic_write_yaml(info.yaml_path, data)
        except Exception as exc:
            log.exception("crop YAML update failed")
            return jsonify({"error": str(exc)}), 500
        return jsonify({
            "ok": True,
            "background_path": "background.png",
            "preview_url": f"/api/themes/{dir_name}/preview?t={int(time.time())}",
        })

    @app.route("/api/themes/<dir_name>/set-video", methods=["POST"])
    def api_theme_set_video(dir_name: str):
        """Switch the theme to use a video background. Body: {filename}.

        Video assets are referenced as-is without cropping. The user is
        expected to pre-rotate / size the file to match the canvas.
        """
        info = manager.get_theme(dir_name)
        if info is None:
            abort(404)
        body = request.get_json(silent=True) or {}
        filename = str(body.get("filename") or "")
        src = (info.yaml_path.parent / filename).resolve()
        try:
            src.relative_to(info.yaml_path.parent.resolve())
        except ValueError:
            abort(403)
        if not src.exists():
            return jsonify({"error": "filename not found in theme dir"}), 404
        # Rename _uploaded.mp4 → video.mp4 so the asset name is stable
        target_name = "video" + src.suffix.lower()
        target = info.yaml_path.parent / target_name
        if src != target:
            shutil.move(str(src), str(target))
        try:
            data = _read_yaml(info.yaml_path)
            data.setdefault("video", {})["path"] = target_name
            data.pop("image", None)
            data["editor_version"] = 1
            data["editor_last_saved"] = datetime.now().isoformat(timespec="seconds")
            _atomic_write_yaml(info.yaml_path, data)
        except Exception as exc:
            log.exception("set-video YAML update failed")
            return jsonify({"error": str(exc)}), 500
        return jsonify({"ok": True, "video_path": target_name})

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
