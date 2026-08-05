#!/usr/bin/env python3
"""Da ortofoto ao mapa, num comando.

    python inventario.py minha_ortofoto.tif --especie cafe

Encadeia as três etapas e abre o navegador no fim:

    1. ingestão   GeoTIFF -> COG (uma vez por ortofoto; se já existir, reaproveita)
    2. inferência COG -> GeoJSON com uma feição por planta
    3. mapa       servidor local mostrando a ortofoto e as detecções

Cada etapa também roda sozinha, se você preferir controlar os parâmetros:

    python ingerir.py  minha_ortofoto.tif
    python inferir.py  minha_ortofoto_cog.tif --especie cafe
    python servidor.py --ortofoto ... --deteccoes ...

Roda sem GPU e sem internet (depois de baixar os modelos). Nada é enviado para
fora desta máquina.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from console import utf8

utf8()

RAIZ = Path(__file__).resolve().parent
SAIDAS = RAIZ / "saidas"


def etapa(n: int, total: int, titulo: str) -> None:
    print(f"\n\033[1m[{n}/{total}] {titulo}\033[0m")


def ja_e_cog(caminho: Path) -> bool:
    """COG bom para este uso = tiled, com overviews. Evita reconverter à toa."""
    import rasterio

    try:
        with rasterio.open(caminho) as src:
            return bool(src.overviews(1)) and src.profile.get("tiled", False)
    except Exception:
        return False


def main() -> None:
    p = argparse.ArgumentParser(
        description="Da ortofoto ao mapa de detecções, num comando.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("ortofoto", type=Path, help="GeoTIFF da ortofoto")
    p.add_argument("--especie", required=True, help="ver as disponíveis em modelos.json")
    p.add_argument("--confianca", type=float, default=0.25)
    p.add_argument("--dedup-m", type=float, default=None,
                   help="padrão: o recomendado para a espécie, em modelos.json")
    p.add_argument("--porta", type=int, default=8000)
    p.add_argument("--sem-navegador", action="store_true")
    p.add_argument("--refazer", action="store_true", help="ignora resultados anteriores")
    a = p.parse_args()

    entrada = a.ortofoto if a.ortofoto.is_absolute() else (Path.cwd() / a.ortofoto)
    if not entrada.is_file():
        raise SystemExit(f"Ortofoto não encontrada: {entrada}")

    from inferir import inferir, resolver_especie

    modelo_path, classes, ficha = resolver_especie(a.especie)
    if not modelo_path.is_file():
        raise SystemExit(
            f"Os pesos de '{a.especie}' não estão em {modelo_path.parent}.\n"
            "Rode antes:  python baixar_modelos.py"
        )

    SAIDAS.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    print(f"\n  \033[1m{ficha['titulo']}\033[0m")
    print(f"  modelo treinado em {ficha['sitio']} — precisão {ficha['precisao']}, "
          f"recall {ficha['recall']} \033[2m(medidos lá, não aqui)\033[0m")

    # 1) ingestão -----------------------------------------------------------
    etapa(1, 3, "Preparando a ortofoto")
    if ja_e_cog(entrada):
        cog = entrada
        print(f"  já está no formato adequado: {cog.name}")
    else:
        cog = SAIDAS / f"{entrada.stem}_cog.tif"
        if cog.is_file() and not a.refazer:
            print(f"  reaproveitando {cog.name}")
        else:
            from ingerir import ingerir

            ingerir(entrada, cog, forcar=True)

    # 2) inferência ---------------------------------------------------------
    etapa(2, 3, "Procurando as plantas")
    base = SAIDAS / f"{entrada.stem}_{a.especie}"
    caixas = base.with_name(base.stem + "_caixas.geojson")
    if caixas.is_file() and not a.refazer:
        import json

        n = len(json.loads(caixas.read_text(encoding="utf-8"))["features"])
        print(f"  reaproveitando {caixas.name} ({n} detecções)")
        print("  use --refazer para rodar de novo")
    else:
        r = inferir(
            ortofoto=cog,
            modelo_path=modelo_path,
            classes=classes,
            saida=base,
            confianca=a.confianca,
            dedup_m=a.dedup_m if a.dedup_m is not None else ficha.get("dedup_m_recomendado", 0.5),
        )
        if not r.get("n"):
            print("\n  Nenhuma planta encontrada. Isso pode significar duas coisas:")
            print("  - a espécie não está nesta ortofoto; ou")
            print("  - este modelo não serve para a sua imagem, o que é comum e esperado.")
            print(f"    Ver 'onde_falha' de '{a.especie}' em modelos.json.")
            caixas = None

    # 3) mapa ---------------------------------------------------------------
    etapa(3, 3, "Abrindo o mapa")
    print(f"  tempo total até aqui: {(time.perf_counter() - t0) / 60:.1f} min")

    from servidor import servir

    servir(cog, caixas, porta=a.porta, abrir=not a.sem_navegador, ficha=ficha)


if __name__ == "__main__":
    main()
