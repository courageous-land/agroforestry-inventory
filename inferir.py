#!/usr/bin/env python3
"""Aplica um modelo de detecção sobre uma ortofoto inteira.

Entra um GeoTIFF (de preferência já convertido para COG por `ingerir.py`), sai um
GeoJSON com uma feição por planta detectada, em EPSG:4326, mais um CSV.

Como funciona: a ortofoto é grande demais para caber num modelo de visão, então
ela é percorrida em recortes com sobreposição. Cada recorte vira uma imagem RGB
esticada para o intervalo 2%-98% (o mesmo pré-processamento usado no treino), o
modelo devolve caixas em pixel, e cada caixa é convertida para coordenada
geográfica pela transformação afim do raster. Como os recortes se sobrepõem, a
mesma planta aparece mais de uma vez perto da borda — daí a deduplicação.

Roda sem GPU. Medido: 100 ms por recorte em CPU contra 17 ms numa RTX 4070, o
que dá cerca de 4 minutos para uma ortofoto de 1,9 GB e 9 minutos para uma de
4 GB. Ter GPU acelera, mas não é requisito.

Uso:
    python inferir.py ortofoto.tif --especie cafe
    python inferir.py ortofoto.tif --modelo modelos/cafe.pth --classes cafe
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from console import utf8

utf8()

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import xy as riot_xy
from rasterio.windows import Window
from tqdm import tqdm

RAIZ = Path(__file__).resolve().parent


# --------------------------------------------------------------------- imagem
def para_rgb_uint8(recorte_chw: np.ndarray, esticar: bool = True) -> np.ndarray:
    """Bandas do raster -> imagem RGB de 8 bits.

    O esticamento 2%-98% por banda é o mesmo aplicado na preparação do conjunto
    de treino. Sem ele, ortofotos com histograma diferente (outro voo, outra
    câmera, outro dia) chegam ao modelo com aparência que ele nunca viu.
    """
    if recorte_chw.shape[0] < 3:
        raise ValueError("O raster precisa de pelo menos 3 bandas (RGB).")
    planos = []
    for b in range(3):
        plano = recorte_chw[b].astype(np.float32)
        if np.all(np.isnan(plano)):
            planos.append(np.zeros(plano.shape, dtype=np.uint8))
            continue
        if not esticar:
            planos.append(np.clip(plano, 0, 255).astype(np.uint8))
            continue
        validos = plano[~np.isnan(plano)]
        if validos.size == 0:
            planos.append(np.zeros(plano.shape, dtype=np.uint8))
            continue
        lo, hi = float(np.percentile(validos, 2.0)), float(np.percentile(validos, 98.0))
        if hi - lo < 1e-3:
            hi = lo + 1.0
        planos.append(np.clip((plano - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8))
    return np.stack(planos, axis=-1)


def completar(arr: np.ndarray, alt: int, larg: int) -> np.ndarray:
    """Completa com preto o recorte da borda, para o modelo receber sempre o mesmo tamanho."""
    h, w = arr.shape[0], arr.shape[1]
    if h >= alt and w >= larg:
        return arr[:alt, :larg]
    saida = np.zeros((alt, larg, 3), dtype=np.uint8)
    saida[:h, :w] = arr
    return saida


def caixa_para_poligono(transform, col_off, linha_off, x1, y1, x2, y2):
    """Caixa em pixel do recorte -> polígono em coordenada do raster."""
    from shapely.geometry import Polygon

    x1, x2 = sorted((float(x1), float(x2)))
    y1, y2 = sorted((float(y1), float(y2)))
    cols = np.array([x1, x2, x2, x1]) + col_off
    linhas = np.array([y1, y1, y2, y2]) + linha_off
    xs, ys = [], []
    for linha, col in zip(linhas, cols):
        px, py = riot_xy(transform, linha, col, offset="ul")
        xs.append(px)
        ys.append(py)
    return Polygon(list(zip(xs, ys)))


# --------------------------------------------------------------- deduplicação
def deduplicar(registros: list[dict], crs, distancia_m: float) -> list[dict]:
    """Remove a mesma planta detectada em recortes vizinhos.

    Mantém, em cada aglomerado, a detecção de maior confiança.

    Usa `cKDTree`. A primeira versão deste código comparava cada ponto com todos
    os outros: 6,0 ms por ponto, o que dava 109 s para as 18 mil detecções brutas
    de uma ortofoto de 1,9 GB, e mais de 200 s numa de 4 GB — tempo de CPU puro
    depois de a inferência já ter acabado. A árvore devolve o mesmo resultado em
    poucos segundos e usa memória proporcional ao número de pontos, e não ao
    quadrado dele.
    """
    if len(registros) <= 1:
        return registros

    import geopandas as gpd
    from scipy.spatial import cKDTree

    gdf = gpd.GeoDataFrame(geometry=[r["centroide"] for r in registros], crs=crs)
    try:
        metrico = gdf.estimate_utm_crs()
    except Exception:
        metrico = crs
    pts = np.array([[g.x, g.y] for g in gdf.to_crs(metrico).geometry])

    arvore = cKDTree(pts)
    suprimidos = np.zeros(len(pts), dtype=bool)
    # da maior confiança para a menor: quem sobrevive elimina os vizinhos
    for i in np.argsort([-r["confianca"] for r in registros]):
        if suprimidos[i]:
            continue
        for j in arvore.query_ball_point(pts[i], r=distancia_m):
            if j != i:
                suprimidos[j] = True
    return [r for k, r in enumerate(registros) if not suprimidos[k]]


# -------------------------------------------------------------------- modelo
def carregar_modelo(caminho: Path):
    """Carrega os pesos do disco."""
    from rfdetr import RFDETRNano

    if not caminho.is_file():
        raise SystemExit(
            f"Modelo não encontrado: {caminho}\n"
            "Rode antes:  python baixar_modelos.py"
        )
    return RFDETRNano(pretrain_weights=str(caminho.resolve()))


def resolver_especie(especie: str) -> tuple[Path, list[str], dict]:
    """Encontra o modelo de uma espécie no manifesto `modelos.json`."""
    manifesto = json.loads((RAIZ / "modelos.json").read_text(encoding="utf-8"))
    for m in manifesto["modelos"]:
        if m["especie"] == especie:
            return RAIZ / "modelos" / m["arquivo"], m["classes"], m
    disponiveis = ", ".join(m["especie"] for m in manifesto["modelos"])
    raise SystemExit(f"Espécie '{especie}' não existe no manifesto.\nDisponíveis: {disponiveis}")


# ------------------------------------------------------------------ execução
def inferir(
    ortofoto: Path,
    modelo_path: Path,
    classes: list[str],
    saida: Path,
    recorte_px: int = 640,
    sobreposicao: float = 0.2,
    confianca: float = 0.25,
    entrada_px: int = 384,
    dedup_m: float = 0.5,
    pular_vazios: bool = True,
    limiar_vazio: float = 12.0,
) -> dict:
    modelo = carregar_modelo(modelo_path)
    passo = max(1, int(recorte_px * (1 - sobreposicao)))
    registros: list[dict] = []
    t0 = time.perf_counter()

    from shapely.geometry import Point

    with rasterio.open(ortofoto) as src:
        if src.count < 3:
            raise SystemExit(f"O raster precisa de ao menos 3 bandas RGB (tem {src.count}).")
        transform, crs = src.transform, src.crs
        n_cols = max(1, (src.width - recorte_px) // passo + 1)
        n_linhas = max(1, (src.height - recorte_px) // passo + 1)
        print(f"[ortofoto] {src.width} x {src.height} px | {crs}")
        print(f"[plano]    {n_linhas * n_cols} recortes de {recorte_px} px, "
              f"{int(sobreposicao * 100)}% de sobreposição")

        barra = tqdm(total=n_linhas * n_cols, desc="recortes", unit="rec")
        for r in range(n_linhas):
            for c in range(n_cols):
                try:
                    col_off, linha_off = c * passo, r * passo
                    dados = src.read(window=Window(col_off, linha_off, recorte_px, recorte_px))
                    if dados.shape[0] < 3 or dados[:3].size == 0:
                        continue
                    alt_real, larg_real = int(dados.shape[1]), int(dados.shape[2])
                    rgb = para_rgb_uint8(dados[:3])
                    # fora do footprint a ortofoto é preta: não vale gastar o modelo
                    if pular_vazios and float(rgb.mean()) < limiar_vazio:
                        continue

                    det = modelo.predict(
                        Image.fromarray(completar(rgb, recorte_px, recorte_px)),
                        threshold=confianca,
                        shape=(entrada_px, entrada_px),
                    )
                    if det is None or len(det) == 0:
                        continue

                    for k in range(len(det.xyxy)):
                        x1, y1, x2, y2 = det.xyxy[k]
                        x1 = min(max(0, x1), max(0, larg_real - 1))
                        x2 = min(max(0, x2), larg_real)
                        y1 = min(max(0, y1), max(0, alt_real - 1))
                        y2 = min(max(0, y2), alt_real)
                        if x2 <= x1 or y2 <= y1:
                            continue
                        poli = caixa_para_poligono(transform, col_off, linha_off, x1, y1, x2, y2)
                        if poli.is_empty or not poli.is_valid:
                            continue
                        cid = int(det.class_id[k])
                        registros.append({
                            "classe": classes[cid] if cid < len(classes) else f"classe_{cid}",
                            "classe_id": cid,
                            "confianca": float(det.confidence[k]),
                            "caixa": poli,
                            "centroide": Point(poli.centroid.x, poli.centroid.y),
                        })
                finally:
                    barra.update(1)
        barra.close()

    print(f"[bruto]    {len(registros)} detecções")
    registros = deduplicar(registros, crs, dedup_m)
    print(f"[dedup]    {len(registros)} após remover repetidas a menos de {dedup_m} m")

    if not registros:
        print("Nenhuma detecção — nada foi gravado.")
        return {"n": 0}

    import geopandas as gpd

    atributos = {
        "classe": [r["classe"] for r in registros],
        "classe_id": [r["classe_id"] for r in registros],
        "confianca": [r["confianca"] for r in registros],
    }
    caixas = gpd.GeoDataFrame(atributos, geometry=[r["caixa"] for r in registros], crs=crs)
    pontos = gpd.GeoDataFrame(atributos, geometry=[r["centroide"] for r in registros], crs=crs)
    if crs and crs.to_epsg() != 4326:
        caixas, pontos = caixas.to_crs(4326), pontos.to_crs(4326)

    saida.parent.mkdir(parents=True, exist_ok=True)
    p_caixas = saida.with_name(saida.stem + "_caixas.geojson")
    p_pontos = saida.with_name(saida.stem + "_centroides.geojson")
    caixas.to_file(p_caixas, driver="GeoJSON")
    pontos.to_file(p_pontos, driver="GeoJSON")

    csv = pontos.copy()
    csv["longitude"] = csv.geometry.x
    csv["latitude"] = csv.geometry.y
    csv.drop(columns="geometry").to_csv(saida.with_suffix(".csv"), index=False)

    dt = time.perf_counter() - t0
    print(f"\n[pronto]   {len(registros)} plantas em {dt / 60:.1f} min")
    print(f"           {p_caixas.name}")
    print(f"           {p_pontos.name}")
    print(f"           {saida.with_suffix('.csv').name}")
    return {"n": len(registros), "caixas": p_caixas, "centroides": p_pontos, "segundos": dt}


def main() -> None:
    p = argparse.ArgumentParser(
        description="Aplica um modelo de detecção sobre uma ortofoto inteira.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("ortofoto", type=Path)
    p.add_argument("--especie", default=None, help="espécie do manifesto modelos.json")
    p.add_argument("--modelo", type=Path, default=None, help="caminho direto para os pesos")
    p.add_argument("--classes", default=None, help="nomes das classes, separados por vírgula")
    p.add_argument("--saida", type=Path, default=None)
    p.add_argument("--recorte-px", type=int, default=640)
    p.add_argument("--sobreposicao", type=float, default=0.2)
    p.add_argument("--confianca", type=float, default=0.25)
    p.add_argument("--dedup-m", type=float, default=0.5,
                   help="distância abaixo da qual duas detecções são a mesma planta. "
                        "Regra prática: metade da menor distância real entre duas plantas.")
    a = p.parse_args()

    if a.especie:
        modelo_path, classes, ficha = resolver_especie(a.especie)
        print(f"[modelo]   {ficha['titulo']} — treinado em {ficha['sitio']}")
        if "precisao" in ficha:
            print(f"           precisão {ficha['precisao']} · recall {ficha['recall']} "
                  f"(medidos em {ficha['sitio']}; ver 'onde_falha' em modelos.json)")
    elif a.modelo:
        modelo_path = a.modelo
        classes = (a.classes or "objeto").split(",")
    else:
        raise SystemExit("Informe --especie (do manifesto) ou --modelo e --classes.")

    saida = a.saida or (RAIZ / "saidas" / f"{a.ortofoto.stem}_{a.especie or 'modelo'}")
    inferir(
        ortofoto=a.ortofoto,
        modelo_path=modelo_path,
        classes=classes,
        saida=saida,
        recorte_px=a.recorte_px,
        sobreposicao=a.sobreposicao,
        confianca=a.confianca,
        dedup_m=a.dedup_m,
    )


if __name__ == "__main__":
    main()
