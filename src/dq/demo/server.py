"""Dependency-free HTTP server for the management demo."""

from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel

from dq.config.settings import ConfigError, load_settings
from dq.demo.service import DemoDataError, NightlyDemoService

MAX_BODY_BYTES = 4096


class DemoService(Protocol):
    def load(self, period: str | None = None) -> Any: ...

    def run(self, period: str) -> Any: ...

    def health(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    content_type: str
    body: bytes


def _json_response(status: int, value: Any) -> Response:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return Response(status, "application/json; charset=utf-8", json.dumps(value).encode())


class DemoApplication:
    """Small routing layer that can be unit-tested without opening a socket."""

    def __init__(self, service: DemoService, static_root: Path | None = None):
        self._service = service
        self._static_root = static_root or Path(str(files("dq.demo").joinpath("static")))

    def dispatch(
        self,
        method: str,
        target: str,
        headers: dict[str, str],
        body: bytes,
    ) -> Response:
        parsed = urlsplit(target)
        try:
            if method == "GET" and parsed.path == "/api/health":
                return _json_response(HTTPStatus.OK, self._service.health())
            if method == "GET" and parsed.path == "/api/nightly-load":
                values = parse_qs(parsed.query, strict_parsing=True) if parsed.query else {}
                unexpected = set(values) - {"period"}
                if unexpected or len(values.get("period", [])) > 1:
                    raise ValueError("only one period query parameter is accepted")
                period = values.get("period", [None])[0]
                return _json_response(HTTPStatus.OK, self._service.load(period))
            if method == "POST" and parsed.path == "/api/nightly-load/run":
                return self._run(headers, body)
            if method == "GET" and not parsed.path.startswith("/api/"):
                return self._static(parsed.path)
        except (DemoDataError, ValueError, json.JSONDecodeError) as exc:
            return _json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except ConfigError as exc:
            return _json_response(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
        except Exception as exc:
            print(f"demo request failed: {type(exc).__name__}")
            return _json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "The demo request failed. Check the server terminal."},
            )
        return _json_response(HTTPStatus.NOT_FOUND, {"error": "route not found"})

    def _run(self, headers: dict[str, str], body: bytes) -> Response:
        if headers.get("x-dq-demo-action") != "run-nightly-load":
            return _json_response(HTTPStatus.FORBIDDEN, {"error": "action header required"})
        if headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise ValueError("content-type must be application/json")
        if len(body) > MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        payload = json.loads(body)
        if not isinstance(payload, dict) or set(payload) != {"period"}:
            raise ValueError("request must contain only period")
        period = payload["period"]
        if not isinstance(period, str):
            raise ValueError("period must be a string")
        return _json_response(HTTPStatus.OK, self._service.run(period))

    def _static(self, request_path: str) -> Response:
        relative = "index.html" if request_path in {"", "/"} else request_path.removeprefix("/")
        allowed = {"index.html", "app.js", "styles.css", "responsive.css"}
        if relative not in allowed:
            return _json_response(HTTPStatus.NOT_FOUND, {"error": "asset not found"})
        path = self._static_root / relative
        if not path.is_file():
            return _json_response(HTTPStatus.NOT_FOUND, {"error": "asset not found"})
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return Response(HTTPStatus.OK, f"{content_type}; charset=utf-8", path.read_bytes())


class DemoRequestHandler(BaseHTTPRequestHandler):
    application: DemoApplication

    def _handle(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        if length > MAX_BODY_BYTES:
            response = _json_response(
                HTTPStatus.BAD_REQUEST, {"error": "request body is too large"}
            )
        else:
            body = self.rfile.read(length) if length else b""
            response = self.application.dispatch(
                self.command,
                self.path,
                {key.lower(): value for key, value in self.headers.items()},
                body,
            )
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'",
        )
        self.end_headers()
        self.wfile.write(response.body)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        print(f"demo {self.address_string()} {format % args}")


def serve(*, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Serve the demo UI and API until interrupted."""
    service = NightlyDemoService(load_settings())

    class ConfiguredDemoHandler(DemoRequestHandler):
        application = DemoApplication(service)

    server = ThreadingHTTPServer((host, port), ConfiguredDemoHandler)
    print(f"DQ Forward demo listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
