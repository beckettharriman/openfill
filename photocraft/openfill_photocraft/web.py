# OpenFill for PhotoCraft
# SPDX-License-Identifier: Apache-2.0
"""The companion's page and JSON API on 127.0.0.1 (standard library only).

Every request must carry the session key the launcher put in the page's URL, so
another local program cannot drive PhotoCraft through it.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from openfill import catalog as cat
from openfill.backend import describe_error
from openfill.config import BACKENDS

from . import __version__
from .service import Service

STATIC = Path(__file__).with_name("static")

# Settings the page may change (the rest stay in config.json).
EDITABLE = {
    "comfy_dir": str,
    "autostart": bool,
    "idle_timeout_minutes": int,
    "extra_server_args": str,
    "steps": int,
    "cfg": float,
    "grow_percent": float,
    "feather_percent": float,
    "debug": bool,
    "backend": str,
    "cloud_provider": str,
    "cloud_model": str,
    "cloud_url": str,
}
# Values a choice setting may take here (anything else is refused, not saved).
CHOICES = {
    "backend": lambda: {b.id for b in BACKENDS if b.photocraft},
    "cloud_provider": lambda: {"openrouter", "openai"},
}


class Api:
    def __init__(self, service: Service) -> None:
        self.service = service
        self.key = secrets.token_urlsafe(24)
        self.on_show = None  # brings the OpenFill bar up (set by the launcher)
        self._pick = {"seq": 0, "action": ""}  # the last Filter menu pick, for the page

    def pick(self, action: str) -> None:
        self._pick = {"seq": self._pick["seq"] + 1, "action": action}

    # GET ------------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        app = self.service.app
        return {
            "version": __version__,
            "job": self.service.state.snapshot(),
            "photocraft": self.service.photocraft(),
            "busy": self.service.busy(),
            "activeModel": "cloud" if app.uses_cloud else app.active_model_id(),
            "comfyReady": _comfy_set(app) or app.uses_cloud,
            "comfyProblem": "" if _comfy_set(app) or app.uses_cloud else _comfy_problem(app),
            "setup": None if _comfy_set(app) or app.uses_cloud else self.service.setup_progress(),
            "pick": self._pick,
            "cloud": _cloud_state(app),
        }

    def models(self) -> dict[str, Any]:
        app = self.service.app
        try:
            rows = app.model_rows(app.server_model_files())
        except Exception as err:  # noqa: BLE001
            return {"rows": [], "error": describe_error(err)}
        status = app.task_status()
        current = status.get("current") or {}
        pending = [t.get("model_id") for t in app.server.pending_tasks()]
        from .modelinfo import hardware_line, needed_by

        for row in rows:
            row["statusText"] = cat.row_status(row, current, pending)
            if not row["runnable"]:
                row["neededBy"] = needed_by(list(app.catalog), row["id"])
        hw = None
        try:
            from openfill import hardware

            detected = hardware.detect(app.models_root, app.server.url())
            hw = hardware_line(detected)
            if detected.is_amd:
                cat.add_amd_notes(rows, app.catalog)  # what may not work on an AMD card
        except Exception:  # noqa: BLE001
            hw = None
        return {"rows": rows, "current": current, "pending": pending, "hardware": hw}

    def thumbnail(self, index: int) -> bytes:
        """A small JPEG of variation ``index`` of the last job (for the page's picker)."""
        layers = self.service.layers
        if not 0 <= index < len(layers):
            return b""
        import io

        from PIL import Image

        layer = layers[index]
        im = Image.frombytes("RGBA", (layer.origin.width, layer.origin.height), layer.rgba).convert("RGB")
        im.thumbnail((320, 240))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue()

    def settings(self) -> dict[str, Any]:
        s = self.service.app.settings
        out = {k: getattr(s, k) for k in EDITABLE}
        # the key itself never leaves the settings file: only whether one is saved, and for what
        out["cloud_key_saved"] = s.cloud_key_usable
        out["cloud_key_elsewhere"] = bool(s.cloud_api_key) and not s.cloud_key_usable
        out["cloud_key_for"] = s.cloud_key_for
        return out

    # POST -----------------------------------------------------------------------------

    def post(self, path: str, body: dict[str, Any]) -> Any:
        svc, app = self.service, self.service.app
        if path in ("/api/fill", "/api/expand", "/api/txt2img"):
            svc.start(path.rsplit("/", 1)[1], body)
            return {"started": True}
        if path == "/api/show":
            if self.on_show:
                self.on_show(str(body.get("action") or "panel"))
            return {"ok": True}
        if path == "/api/open":
            return svc.bridge.open_file(str(body["path"]))
        if path == "/api/cancel":
            svc.cancel()
            return {"ok": True}
        if path == "/api/variation":
            svc.show_variation(int(body.get("index", 0)))
            return {"ok": True}
        if path == "/api/keep":
            svc.keep(str(body.get("which", "selected")))
            return {"ok": True}
        if path == "/api/models/download":
            return {"tasks": app.queue_download(str(body["id"]))}
        if path == "/api/models/activate":
            entry = app.set_active(str(body["id"]))
            return {"active": entry.id}
        if path == "/api/models/remove":
            return {"task": svc.uninstall(str(body["id"]))}
        if path == "/api/models/stop":
            return {"stopped": svc.stop_download(str(body["id"]))}
        if path == "/api/models/nodes":
            return {"task": app.queue_node_install(str(body["id"]))}
        if path == "/api/models/token":
            app.save_token(str(body.get("token", "")))
            return {"ok": True}
        if path == "/api/settings":
            for key, allowed in CHOICES.items():
                if key in body and str(body[key]) not in allowed():
                    raise ValueError(f"'{body[key]}' is not a choice for {key}.")
            was_cloud = app.uses_cloud
            from dataclasses import replace

            # checked on a copy first: a refused save changes nothing
            trial = replace(app.settings)
            for key, kind in EDITABLE.items():
                if key in body:
                    setattr(trial, key, kind(body[key]))
            # a new key is bound to this service and address; an empty field never moves the saved one
            trial.set_cloud(
                trial.cloud_provider, trial.cloud_url, trial.cloud_model, str(body.get("cloud_api_key") or "")
            )
            if trial.uses_cloud and not trial.cloud_key_usable:
                raise ValueError(trial.cloud_key_problem() or "Paste your API key for the cloud service first.")
            app.settings.__dict__.update(trial.__dict__)
            app.settings.save(app.paths.config)
            app.reload_settings()
            if was_cloud and not app.uses_cloud:
                svc.warm_up()
            return self.settings()
        if path == "/api/comfy/setup":
            return {"task": svc.setup_comfy()}
        if path == "/api/backend/check":
            health = app.check_backend()
            return {"ok": health.ok, "detail": health.detail, "device": getattr(health, "device", "")}
        if path == "/api/backend/stop":
            return {"message": app.stop_backend()}
        if path == "/api/logs":
            app.open_log_folder()
            return {"ok": True}
        raise KeyError(path)


def _cloud_state(app) -> dict[str, Any] | None:
    """What the page shows while the cloud is the backend (None otherwise); never the key."""
    if not app.uses_cloud:
        return None
    from openfill.backends.cloud import destination

    s = app.settings
    return {
        "status": f"Cloud: {destination(s.cloud_provider, s.cloud_url)}",
        "note": s.cloud_note(),
        "promptNote": s.cloud_note("prompt"),
        "model": app.cloud_model_name(),
        "key": s.cloud_key_usable,
        "keyProblem": s.cloud_key_problem(),
    }


def _comfy_set(app) -> bool:
    """A ComfyUI folder OpenFill can use (models and nodes live there), not just a server answering."""
    from openfill.config import comfy_layout

    return comfy_layout(app.settings.comfy_dir) is not None


def _comfy_problem(app) -> str:
    from openfill.config import comfy_problem

    return comfy_problem(app.settings.comfy_dir) or "ComfyUI is not set up."


def make_handler(api: Api) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "OpenFillPhotoCraft"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet console
            pass

        def _authorised(self) -> bool:
            query = parse_qs(urlparse(self.path).query)
            return secrets.compare_digest((query.get("key") or [""])[0], api.key) or secrets.compare_digest(
                self.headers.get("X-OpenFill-Key", ""), api.key
            )

        def _send(self, code: int, body: bytes, kind: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data: Any) -> None:
            self._send(code, json.dumps(data, default=str).encode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if not self._authorised():
                self._send(403, b"Open OpenFill from its launcher (the address carries a key).", "text/plain")
                return
            try:
                if path in ("/", "/index.html"):
                    self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                elif path == "/api/ping":
                    self._json(200, {"ok": True, "version": __version__})
                elif path == "/api/state":
                    self._json(200, api.state())
                elif path == "/api/models":
                    self._json(200, api.models())
                elif path == "/api/settings":
                    self._json(200, api.settings())
                elif path == "/api/comfy/preflight":  # what Download ComfyUI would fetch (read-only)
                    self._json(200, api.service.comfy_preflight())
                elif path == "/api/thumb":
                    data = api.thumbnail(int((parse_qs(urlparse(self.path).query).get("i") or ["0"])[0]))
                    if data:
                        self._send(200, data, "image/jpeg")
                    else:
                        self._send(204, b"", "text/plain")
                elif path == "/api/preview":
                    st = api.service.state
                    with st.lock:
                        data, mime = st.preview, st.preview_mime or "image/jpeg"
                    if not data:
                        self._send(204, b"", "text/plain")
                    else:
                        self._send(200, data, mime)
                else:
                    self._send(404, b"not found", "text/plain")
            except Exception as err:  # noqa: BLE001
                self._json(500, {"error": describe_error(err)})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorised():
                self._send(403, b"forbidden", "text/plain")
                return
            length = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("expected a JSON object")
                self._json(200, api.post(urlparse(self.path).path, body))
            except KeyError as err:
                self._json(404, {"error": f"unknown request {err}"})
            except Exception as err:  # noqa: BLE001
                self._json(400, {"error": describe_error(err) if not isinstance(err, RuntimeError) else str(err)})

    return Handler


def serve(api: Api, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Start the page on 127.0.0.1 in a background thread; returns (server, url with key)."""
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(api))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="openfill-web", daemon=True).start()
    host, real_port = server.server_address[:2]
    return server, f"http://{host}:{real_port}/?key={api.key}"
