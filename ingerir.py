#!/usr/bin/env python3
"""Ingestão de ortofoto: GeoTIFF qualquer -> COG pronto para uso.

Converte a ortofoto para Cloud Optimized GeoTIFF: organizada em blocos,
comprimida sem perda e com uma pirâmide interna de resoluções.

Por que isso importa: sem essa preparação, ler um pedaço da imagem obriga o
programa a percorrer o arquivo inteiro. Com ela, tanto a inferência quanto o
mapa leem só o pedaço de que precisam. É o que permite servir o mapa sem gerar
uma pirâmide de imagens em disco — o que custaria centenas de megabytes e
vários minutos por ortofoto.

A conversão usa a API do rasterio, sem depender de nenhum binário externo no
PATH, então roda igual em Windows, Linux e Mac.

O arquivo de entrada NÃO é modificado.

A profundidade da pirâmide é calculada a partir das dimensões da imagem, e não
fixada: uma ortofoto de 4 GB precisa descer até 1/128 para que o zoom mais
afastado continue lendo de um nível pequeno. Listas fixas de níveis, comuns em
tutoriais, funcionam para a imagem em que foram escritas e param de funcionar
quando a imagem dobra de tamanho.

Exemplos:
  python ingerir.py minha_ortofoto.tif
  python ingerir.py voo_novo.tif --saida saidas/voo_novo_cog.tif
  python ingerir.py entrada.tif --recorte 4096   # amostra pequena, para testar
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from console import utf8

utf8()

import rasterio
from rasterio.enums import Resampling
from rasterio.shutil import copy as rio_copy
from rasterio.windows import Window

RAIZ = Path(__file__).resolve().parent

# lado do bloco interno do COG. 512 px é o tamanho que faz um tile de mapa de
# 256 px caber sempre dentro de um único bloco, então cada requisição do mapa lê
# o mínimo possível do disco.
BLOCO = 512


def niveis_de_overview(largura: int, altura: int, bloco: int = BLOCO) -> list[int]:
    """Potências de 2 até o menor overview caber num bloco.

    É a mesma regra que o driver COG aplica internamente; calculamos aqui só
    para poder informar ao usuário e conferir depois da escrita.
    """
    niveis, fator = [], 2
    while max(largura, altura) // fator >= bloco:
        niveis.append(fator)
        fator *= 2
    return niveis


def humano(n_bytes: int) -> str:
    v = float(n_bytes)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or u == "TB":
            return f"{v:.1f} {u}"
        v /= 1024
    return f"{v:.1f} TB"


def recortar(entrada: Path, destino: Path, lado: int) -> Path:
    """Salva um recorte quadrado do centro. Serve para testar a ingestão rápido."""
    with rasterio.open(entrada) as src:
        lado = min(lado, src.width, src.height)
        col = (src.width - lado) // 2
        row = (src.height - lado) // 2
        win = Window(col, row, lado, lado)
        perfil = src.profile.copy()
        perfil.update(
            width=lado,
            height=lado,
            transform=src.window_transform(win),
            compress="deflate",
            tiled=True,
            blockxsize=BLOCO,
            blockysize=BLOCO,
        )
        perfil.pop("photometric", None)
        with rasterio.open(destino, "w", **perfil) as dst:
            dst.write(src.read(window=win))
    print(f"[recorte] {lado}x{lado} px do centro -> {destino.name} ({humano(destino.stat().st_size)})")
    return destino


def ingerir(entrada: Path, saida: Path, forcar: bool) -> dict:
    if not entrada.is_file():
        raise SystemExit(f"Ortofoto não encontrada: {entrada}")
    if saida.exists() and not forcar:
        raise SystemExit(f"{saida} já existe. Use --forcar para sobrescrever.")

    with rasterio.open(entrada) as src:
        larg, alt, bandas = src.width, src.height, src.count
        crs, res = src.crs, src.res
        if bandas < 3:
            raise SystemExit(f"O raster precisa de ao menos 3 bandas (tem {bandas}).")
        overviews_orig = src.overviews(1)

    niveis = niveis_de_overview(larg, alt)
    megapixels = larg * alt / 1e6
    print(f"[entrada] {entrada.name}")
    print(f"          {larg} x {alt} px ({megapixels:.0f} Mpx), {bandas} bandas, {crs}")
    print(f"          GSD ~ {res[0]:.4f} x {res[1]:.4f} (unidade do CRS)")
    print(f"          tamanho {humano(entrada.stat().st_size)}, overviews existentes: {overviews_orig or 'nenhum'}")
    print(f"[plano]   overviews até 1/{niveis[-1] if niveis else 1} "
          f"(menor nível ~{max(larg, alt) // (niveis[-1] if niveis else 1)} px)")

    saida.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    # O driver COG do GDAL (>= 3.1) escreve o arquivo já em blocos, comprimido e
    # com a pirâmide interna, numa única passada — e pela biblioteca, sem chamar
    # nenhum executável externo.
    with rasterio.open(entrada) as src:
        rio_copy(
            src,
            str(saida),
            driver="COG",
            BLOCKSIZE=BLOCO,
            COMPRESS="DEFLATE",
            PREDICTOR="YES",           # o driver COG escolhe 2 ou 3 pelo dtype
            LEVEL=6,
            BIGTIFF="YES",             # obrigatório acima de 4 GB, inofensivo abaixo
            NUM_THREADS="ALL_CPUS",
            OVERVIEW_RESAMPLING="AVERAGE",
        )

    dt = time.perf_counter() - t0

    with rasterio.open(saida) as dst:
        ov = dst.overviews(1)
        bloco_real = dst.block_shapes[0]
        menor = max(dst.width, dst.height) // (ov[-1] if ov else 1)

    tam = saida.stat().st_size
    print(f"[saída]   {saida}")
    print(f"          {humano(tam)} ({tam / entrada.stat().st_size * 100:.0f}% do original), "
          f"bloco {bloco_real[0]}x{bloco_real[1]}")
    print(f"          overviews {ov} -> menor nível ~{menor} px")
    print(f"[tempo]   {dt:.1f} s")

    if not ov:
        print("[AVISO]   o COG saiu sem overviews; o zoom baixo vai ler o nível cheio.")
    return {
        "arquivo": str(saida),
        "largura": larg,
        "altura": alt,
        "bandas": bandas,
        "crs": str(crs),
        "overviews": ov,
        "bytes": tam,
        "segundos": round(dt, 1),
    }


def main() -> None:
    p = argparse.ArgumentParser(
        description="Converte uma ortofoto GeoTIFF em COG, pronta para inferência e para o mapa.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("entrada", type=Path, help="GeoTIFF de entrada (não é modificado)")
    p.add_argument("--saida", type=Path, default=None,
                   help="COG de saída (padrão: <entrada>_cog.tif ao lado da entrada)")
    p.add_argument("--forcar", action="store_true", help="sobrescreve a saída se já existir")
    p.add_argument("--recorte", type=int, default=0, metavar="PX",
                   help="antes de converter, recorta um quadrado de PX px do centro (para teste rápido)")
    args = p.parse_args()

    entrada = args.entrada if args.entrada.is_absolute() else (RAIZ / args.entrada)
    saida = args.saida or entrada.with_name(f"{entrada.stem}_cog.tif")
    if not saida.is_absolute():
        saida = RAIZ / saida

    if args.recorte:
        temp = saida.with_name(f"{saida.stem}_recorte_bruto.tif")
        entrada = recortar(entrada, temp, args.recorte)

    ingerir(entrada, saida, args.forcar)

    if args.recorte and entrada.name.endswith("_recorte_bruto.tif"):
        entrada.unlink(missing_ok=True)

    print(f"\nPróximo passo:  python inferir.py {saida.name} --especie <especie>")


if __name__ == "__main__":
    main()
