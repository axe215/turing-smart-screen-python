"""Flask app factory for the theme-manager dashboard.

Endpoints:
  GET  /                          → dashboard HTML
  GET  /api/themes                → list available themes (JSON)
  GET  /api/status                → manager + engine status (JSON)
  POST /api/start                 → {dir_name, params} start engine
  POST /api/stop                  → stop engine
  GET  /themes/<dir>/preview      → preview image if present

The Flask app holds a reference to a ThemeManager instance — that's
the only mutable state. The UI polls /api/status every couple seconds
to refresh.
"""
from __future__ import annotations

import logging
from pathlib import Path

from flask import (
    Flask,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
)

from ..manager import EngineParams, ThemeManager

log = logging.getLogger(__name__)


def create_app(manager: ThemeManager) -> Flask:
    here = Path(__file__).resolve().parent
    app = Flask(
        __name__,
        template_folder=str(here / "templates"),
        static_folder=str(here / "static"),
    )
    app.config["manager"] = manager

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/themes")
    def api_themes():
        return jsonify([t.to_dict() for t in manager.list_themes()])

    @app.route("/api/status")
    def api_status():
        return jsonify(manager.status())

    @app.route("/api/start", methods=["POST"])
    def api_start():
        body = request.get_json(silent=True) or {}
        dir_name = body.get("dir_name")
        if not dir_name:
            return jsonify({"error": "dir_name required"}), 400
        params_in = body.get("params") or {}
        # Build EngineParams, allowing partial override of defaults
        cur = manager.params
        params = EngineParams(
            rotate_180=bool(params_in.get("rotate_180", cur.rotate_180)),
            rotate_video=int(params_in.get("rotate_video", cur.rotate_video)),
            font_scale=float(params_in.get("font_scale", cur.font_scale)),
            widget_period=float(params_in.get("widget_period", cur.widget_period)),
            screen=str(params_in.get("screen", cur.screen)),
        )
        try:
            manager.start(dir_name, params=params)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:
            log.exception("start failed")
            return jsonify({"error": str(exc)}), 500
        return jsonify(manager.status())

    @app.route("/api/stop", methods=["POST"])
    def api_stop():
        manager.stop()
        return jsonify(manager.status())

    @app.route("/themes/<dir_name>/preview")
    def theme_preview(dir_name: str):
        info = manager.get_theme(dir_name)
        if info is None or info.preview_path is None:
            abort(404)
        return send_file(str(info.preview_path))

    return app
