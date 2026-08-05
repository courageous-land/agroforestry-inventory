"""Tiles servidos direto do COG, sob demanda, com rio-tiler.

Dispensa a pirâmide de imagens que normalmente se gera antes de exibir uma
ortofoto na web — que numa imagem de 1,9 GB custa 608 MB em disco e alguns
minutos de processamento, e numa de 4 GB passa de 1,3 GB.

Aqui o custo é O(tile) e não O(imagem): o recorte é lido do overview de
resolução mais próxima, então o zoom baixo — que cobre mais área — é o mais
barato, não o mais caro. Medido na ortofoto real: 18–37 ms por tile de z16 a
z23. Numa ortofoto de 4 GB o número é o mesmo, desde que os overviews desçam
fundo o bastante (é o que o `ingerir.py` garante).

Duas armadilhas tratadas aqui:

  * **Threads.** O servidor é `ThreadingHTTPServer`. Um `DatasetReader` do
    rasterio não pode ser lido por duas threads ao mesmo tempo — o resultado é
    tile corrompido ou queda. Cada thread ganha o seu leitor (`threading.local`).

  * **Esquema de eixo.** Há duas convenções para numerar tiles, XYZ e TMS, que
    diferem pelo sentido do eixo Y: `y_tms = 2**z - 1 - y_xyz`. Trocar uma pela
    outra espelha o mapa verticalmente sem dar erro nenhum. Quem chama diz qual
    quer, e o visualizador pede TMS.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from rasterio.crs import CRS
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import Reader

# PNG de 256x256 totalmente transparente, devolvido quando o tile cai fora do
# footprint. Evita 404 em série: o MapLibre pede a caixa inteira mesmo com
# `bounds` definido, e um 404 por tile polui o console do navegador.
_VAZIO: bytes | None = None


def png_vazio() -> bytes:
    """PNG 256x256 RGBA totalmente transparente.

    Montado com PIL de propósito. A primeira versão usava `ImageData(z, m)` do
    rio-tiler com a máscara zerada, esperando "0 = transparente" — mas a
    convenção de máscara mudou entre versões, e o resultado foi um tile **preto
    opaco**, que pintaria de preto toda a área em volta da ortofoto. Aqui os
    quatro canais são escritos à mão e não há convenção para errar.
    """
    global _VAZIO
    if _VAZIO is None:
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(buf, format="PNG")
        _VAZIO = buf.getvalue()
    return _VAZIO


def y_para_xyz(z: int, y: int) -> int:
    """Converte Y do esquema TMS para XYZ (a operação é a sua própria inversa)."""
    return (1 << z) - 1 - y


class Piramide:
    """Um COG servido como pirâmide de tiles, com cache em disco.

    O cache é opcional mas recomendado: a primeira visita a uma região paga os
    ~20 ms de leitura, as seguintes leem do disco. A chave inclui tamanho e
    mtime do raster, então reingerir a ortofoto invalida o cache sozinho.
    """

    def __init__(self, caminho: Path, cache_dir: Path | None = None,
                 reamostragem: str = "cubic") -> None:
        self.caminho = Path(caminho)
        if not self.caminho.is_file():
            raise FileNotFoundError(f"COG não encontrado: {self.caminho}")

        # `cubic` foi escolhido por medição: numa comparação pixel a pixel de 40
        # tiles entre z18 e z21 contra uma pirâmide gerada pelo método clássico,
        # foi o que menos diferiu (7,97/255 de diferença média, contra 9,55 do
        # `nearest`, que é o padrão da biblioteca). O alinhamento é exato em
        # ambos; o que sobra é diferença de reamostragem, invisível a olho.
        self.reamostragem = reamostragem
        self._local = threading.local()
        st = self.caminho.stat()
        chave = f"{self.caminho.resolve()}|{st.st_size}|{int(st.st_mtime)}"
        self.assinatura = hashlib.sha1(chave.encode("utf-8")).hexdigest()[:16]
        self.cache_dir = (cache_dir / self.assinatura) if cache_dir else None

        # os limites vêm do próprio COG; o frontend precisa deles para não pedir
        # tiles do mundo inteiro
        with Reader(str(self.caminho)) as r:
            self.minzoom = int(r.minzoom)
            self.maxzoom = int(r.maxzoom)
            self.bounds_4326 = tuple(
                round(v, 8) for v in r.get_geographic_bounds(CRS.from_epsg(4326))
            )

    # -- leitor por thread --------------------------------------------------
    @property
    def _leitor(self) -> Reader:
        r = getattr(self._local, "reader", None)
        if r is None:
            r = Reader(str(self.caminho))
            self._local.reader = r
        return r

    def fechar(self) -> None:
        r = getattr(self._local, "reader", None)
        if r is not None:
            r.close()
            self._local.reader = None

    # -- tiles --------------------------------------------------------------
    def _caminho_cache(self, z: int, x: int, y: int) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / str(z) / str(x) / f"{y}.png"

    def tile(self, z: int, x: int, y: int, esquema: str = "xyz") -> bytes:
        """PNG do tile. Fora do footprint devolve PNG transparente, não erro."""
        if esquema == "tms":
            y = y_para_xyz(z, y)

        alvo = self._caminho_cache(z, x, y)
        if alvo and alvo.is_file():
            return alvo.read_bytes()

        try:
            img = self._leitor.tile(x, y, z, resampling_method=self.reamostragem)
        except TileOutsideBounds:
            return png_vazio()

        png = img.render(img_format="PNG")
        if alvo:
            alvo.parent.mkdir(parents=True, exist_ok=True)
            tmp = alvo.with_suffix(".tmp")
            tmp.write_bytes(png)
            tmp.replace(alvo)   # atômico: duas threads podem pedir o mesmo tile
        return png

    def info(self) -> dict:
        return {
            "arquivo": self.caminho.name,
            "minzoom": self.minzoom,
            "maxzoom": self.maxzoom,
            "bounds_4326": list(self.bounds_4326),
            "reamostragem": self.reamostragem,
            "cache": str(self.cache_dir) if self.cache_dir else None,
        }


class Piramides:
    """Cache de `Piramide` por caminho de raster.

    Vários projetos podem apontar para a mesma ortofoto; abrir uma vez só evita
    reler os metadados e multiplicar leitores.
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir
        self._por_caminho: dict[str, Piramide] = {}
        self._trava = threading.Lock()

    def obter(self, caminho: Path) -> Piramide:
        chave = str(Path(caminho).resolve())
        with self._trava:
            p = self._por_caminho.get(chave)
            if p is None:
                p = Piramide(Path(caminho), self.cache_dir)
                self._por_caminho[chave] = p
            return p
