#!/usr/bin/env python3
"""Local server: shows the orthophoto and the detections on a map, in the browser.

Standard library only for the HTTP part. The orthophoto is not converted into a
pyramid of image files: each tile is cut from the COG on request (see `tiles.py`),
which saves gigabytes of intermediate files and lets the first view start
immediately.

Nothing leaves this machine. There is no API key, no external service, and the
map library ships inside the repository - it works with no internet.

Routes:
    GET /                        the page
    GET /cog/{z}/{x}/{y}.png     tile cut from the orthophoto
    GET /api/info                bounds, zoom, count and the model card
    GET /api/detections          the detections, as GeoJSON

Usage (normally called by `inventory.py`):
    python server.py --orthophoto outputs/x_cog.tif --detections outputs/x_boxes.geojson
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
TILE_ROUTE = re.compile(r"^/cog/(\d+)/(\d+)/(\d+)\.png$")


class Handler(BaseHTTPRequestHandler):
    pyramid = None       # tiles.Pyramid
    detections = None    # Path to the GeoJSON
    card = None          # entry from models.json
    protocol_version = "HTTP/1.1"
    server_version = "AgroforestryInventory/1.0"

    def log_message(self, fmt, *args):
        if "/cog/" not in (self.path or ""):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, kind: str, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _file(self, path: Path, cache: str = "no-store") -> None:
        if not path or not path.is_file():
            self._json({"error": f"not found: {self.path}"}, 404)
            return
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind in ("application/javascript", "application/json"):
            kind += "; charset=utf-8"
        self._send(200, path.read_bytes(), kind, cache)

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path

        m = TILE_ROUTE.match(route)
        if m:
            z, x, y = (int(v) for v in m.groups())
            try:
                png = self.pyramid.tile(z, x, y, scheme="tms")
            except Exception as e:
                traceback.print_exc()
                self._json({"error": f"tile {z}/{x}/{y} failed: {e}"}, 500)
                return
            self._send(200, png, "image/png", cache="public, max-age=31536000")
            return

        if route == "/api/info":
            n = 0
            if self.detections and self.detections.is_file():
                try:
                    n = len(json.loads(self.detections.read_text(encoding="utf-8"))["features"])
                except (ValueError, KeyError):
                    n = 0
            self._json({**self.pyramid.info(), "scheme": "tms",
                        "n_detections": n, "model": self.card})
            return

        if route == "/api/detections":
            if self.detections and self.detections.is_file():
                self._file(self.detections)
            else:
                self._json({"type": "FeatureCollection", "features": []})
            return

        target = (WEB / ("index.html" if route == "/" else route.lstrip("/"))).resolve()
        if not str(target).startswith(str(WEB.resolve())):
            self._json({"error": "outside the web directory"}, 403)
            return
        self._file(target)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()


def serve(orthophoto: Path, detections: Path | None, port: int = 8000,
          open_browser: bool = True, card: dict | None = None,
          address: str = "127.0.0.1") -> None:
    """Start the map.

    The default is `127.0.0.1`, which accepts connections from this machine only:
    the user's orthophoto is not exposed to the local network by accident. Inside
    a container that would make the service unreachable, so whoever publishes
    passes `address="0.0.0.0"` explicitly - a deliberate choice, not a default.
    """
    from tiles import Pyramid

    Handler.pyramid = Pyramid(orthophoto, cache_dir=ROOT / "cache")
    Handler.detections = detections
    Handler.card = card

    httpd = ThreadingHTTPServer((address, port), Handler)
    url = f"http://{'127.0.0.1' if address == '0.0.0.0' else address}:{port}/"
    print(f"\n  Map running at {url}")
    print("  Ctrl+C to stop.\n")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


def main() -> None:
    p = argparse.ArgumentParser(description="Show the orthophoto and detections in the browser.")
    p.add_argument("--orthophoto", type=Path, required=True, help="COG produced by ingest.py")
    p.add_argument("--detections", type=Path, default=None, help="boxes GeoJSON")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args()
    if not a.orthophoto.is_file():
        raise SystemExit(f"Orthophoto not found: {a.orthophoto}")
    serve(a.orthophoto, a.detections, a.port, not a.no_browser)


if __name__ == "__main__":
    main()
