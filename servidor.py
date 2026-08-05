#!/usr/bin/env python3
"""Servidor local: mostra a ortofoto e as detecções num mapa, no navegador.

Só biblioteca padrão para o HTTP. A ortofoto não é convertida numa pirâmide de
imagens: cada tile é recortado do COG na hora (ver `tiles.py`), o que dispensa
gigabytes de arquivos intermediários e faz a primeira visualização começar
imediatamente.

Nada sai desta máquina. Não há chave de API, não há serviço externo, e a
biblioteca de mapa está no próprio repositório — funciona sem internet.

Rotas:
    GET /                        a página
    GET /cog/{z}/{x}/{y}.png     tile recortado da ortofoto
    GET /api/info                limites, zoom, contagem e ficha do modelo
    GET /api/deteccoes           as detecções, em GeoJSON

Uso (normalmente chamado pelo `inventario.py`):
    python servidor.py --ortofoto saidas/x_cog.tif --deteccoes saidas/x_caixas.geojson
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

RAIZ = Path(__file__).resolve().parent
WEB = RAIZ / "web"
ROTA_TILE = re.compile(r"^/cog/(\d+)/(\d+)/(\d+)\.png$")


class Handler(BaseHTTPRequestHandler):
    piramide = None      # tiles.Piramide
    deteccoes = None     # Path do GeoJSON
    ficha = None         # entrada do modelos.json usada
    protocol_version = "HTTP/1.1"
    server_version = "AgroforestryInventory/1.0"

    def log_message(self, fmt, *args):
        if "/cog/" not in (self.path or ""):
            super().log_message(fmt, *args)

    # -- helpers ------------------------------------------------------------
    def _envia(self, code: int, corpo: bytes, tipo: str, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(corpo)

    def _json(self, obj, code: int = 200) -> None:
        self._envia(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8")

    def _arquivo(self, caminho: Path, cache: str = "no-store") -> None:
        if not caminho or not caminho.is_file():
            self._json({"erro": f"não encontrado: {self.path}"}, 404)
            return
        tipo = mimetypes.guess_type(caminho.name)[0] or "application/octet-stream"
        if tipo.startswith("text/") or tipo in ("application/javascript", "application/json"):
            tipo += "; charset=utf-8"
        self._envia(200, caminho.read_bytes(), tipo, cache)

    # -- rotas --------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        caminho = urlparse(self.path).path

        m = ROTA_TILE.match(caminho)
        if m:
            z, x, y = (int(v) for v in m.groups())
            try:
                png = self.piramide.tile(z, x, y, esquema="tms")
            except Exception as e:
                traceback.print_exc()
                self._json({"erro": f"falha no tile {z}/{x}/{y}: {e}"}, 500)
                return
            self._envia(200, png, "image/png", cache="public, max-age=31536000")
            return

        if caminho == "/api/info":
            n = 0
            if self.deteccoes and self.deteccoes.is_file():
                try:
                    n = len(json.loads(self.deteccoes.read_text(encoding="utf-8"))["features"])
                except (ValueError, KeyError):
                    n = 0
            self._json({
                **self.piramide.info(),
                "esquema": "tms",
                "n_deteccoes": n,
                "modelo": self.ficha,
            })
            return

        if caminho == "/api/deteccoes":
            if self.deteccoes and self.deteccoes.is_file():
                self._arquivo(self.deteccoes)
            else:
                self._json({"type": "FeatureCollection", "features": []})
            return

        alvo = (WEB / ("index.html" if caminho == "/" else caminho.lstrip("/"))).resolve()
        if not str(alvo).startswith(str(WEB.resolve())):
            self._json({"erro": "fora do diretório web"}, 403)
            return
        self._arquivo(alvo)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()


def servir(ortofoto: Path, deteccoes: Path | None, porta: int = 8000,
           abrir: bool = True, ficha: dict | None = None) -> None:
    from tiles import Piramide

    Handler.piramide = Piramide(ortofoto, cache_dir=RAIZ / "cache")
    Handler.deteccoes = deteccoes
    Handler.ficha = ficha

    servidor = ThreadingHTTPServer(("127.0.0.1", porta), Handler)
    url = f"http://127.0.0.1:{porta}/"
    print(f"\n  Mapa no ar: {url}")
    print("  Ctrl+C para encerrar.\n")
    if abrir:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado.")
    finally:
        servidor.server_close()


def main() -> None:
    p = argparse.ArgumentParser(description="Mostra a ortofoto e as detecções no navegador.")
    p.add_argument("--ortofoto", type=Path, required=True, help="COG gerado por ingerir.py")
    p.add_argument("--deteccoes", type=Path, default=None, help="GeoJSON de caixas")
    p.add_argument("--porta", type=int, default=8000)
    p.add_argument("--sem-navegador", action="store_true")
    a = p.parse_args()
    if not a.ortofoto.is_file():
        raise SystemExit(f"Ortofoto não encontrada: {a.ortofoto}")
    servir(a.ortofoto, a.deteccoes, a.porta, not a.sem_navegador)


if __name__ == "__main__":
    main()
