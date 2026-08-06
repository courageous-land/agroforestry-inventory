#!/usr/bin/env python3
"""Run a detection model over a whole orthophoto.

Takes a GeoTIFF (ideally already converted by `ingest.py`) and writes one GeoJSON
feature per detected plant, in EPSG:4326, plus a CSV.

How it works: an orthophoto is far too large to feed to a vision model in one
piece, so it is walked in overlapping tiles. Each tile becomes an RGB image
stretched to its 2nd-98th percentile - the same preprocessing used in training -
the model returns boxes in pixel space, and each box is converted to geographic
coordinates through the raster's affine transform. Because the tiles overlap, a
plant near a tile edge is detected more than once; that is what the
de-duplication step removes.

Runs without a GPU. Measured: 100 ms per tile on CPU against 17 ms on an
RTX 4070, which is about 4 minutes for a 1.9 GB orthophoto and 9 minutes for a
4 GB one. A GPU helps but is not required.

Usage:
    python detect.py orthophoto.tif --species coffee
    python detect.py orthophoto.tif --model models/cafe.pth --classes coffee
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from console import utf8

utf8()

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import xy as raster_xy
from rasterio.windows import Window
from tqdm import tqdm

ROOT = Path(__file__).resolve().parent


# --------------------------------------------------------------------- imagery
def to_rgb_uint8(tile_chw: np.ndarray, stretch: bool = True) -> np.ndarray:
    """Raster bands -> 8-bit RGB.

    The 2%-98% per-band stretch is the same one applied when the training set was
    built. Without it, an orthophoto with a different histogram - another flight,
    another camera, another day - reaches the model looking like nothing it saw.
    """
    if tile_chw.shape[0] < 3:
        raise ValueError("The raster needs at least 3 bands (RGB).")
    planes = []
    for b in range(3):
        plane = tile_chw[b].astype(np.float32)
        if np.all(np.isnan(plane)):
            planes.append(np.zeros(plane.shape, dtype=np.uint8))
            continue
        if not stretch:
            planes.append(np.clip(plane, 0, 255).astype(np.uint8))
            continue
        valid = plane[~np.isnan(plane)]
        if valid.size == 0:
            planes.append(np.zeros(plane.shape, dtype=np.uint8))
            continue
        lo, hi = float(np.percentile(valid, 2.0)), float(np.percentile(valid, 98.0))
        if hi - lo < 1e-3:
            hi = lo + 1.0
        planes.append(np.clip((plane - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8))
    return np.stack(planes, axis=-1)


def pad(arr: np.ndarray, height: int, width: int) -> np.ndarray:
    """Pad an edge tile with black so the model always gets the same size."""
    h, w = arr.shape[0], arr.shape[1]
    if h >= height and w >= width:
        return arr[:height, :width]
    out = np.zeros((height, width, 3), dtype=np.uint8)
    out[:h, :w] = arr
    return out


def box_to_polygon(transform, col_off, row_off, x1, y1, x2, y2):
    """Pixel box within a tile -> polygon in raster coordinates."""
    from shapely.geometry import Polygon

    x1, x2 = sorted((float(x1), float(x2)))
    y1, y2 = sorted((float(y1), float(y2)))
    cols = np.array([x1, x2, x2, x1]) + col_off
    rows = np.array([y1, y1, y2, y2]) + row_off
    xs, ys = [], []
    for row, col in zip(rows, cols):
        px, py = raster_xy(transform, row, col, offset="ul")
        xs.append(px)
        ys.append(py)
    return Polygon(list(zip(xs, ys)))


# ------------------------------------------------------------- de-duplication
def deduplicate(records: list[dict], crs, distance_m: float) -> list[dict]:
    """Remove the same plant detected in neighbouring tiles.

    Keeps the highest-confidence detection in each cluster.

    Uses a k-d tree. The first version of this code compared every point against
    every other: 6.0 ms per point, which is 109 s for the 18,000 raw detections of
    a 1.9 GB orthophoto and over 200 s for a 4 GB one - pure CPU time after the
    model had already finished. The tree returns the same result in seconds and
    uses memory proportional to the number of points rather than to its square.
    """
    if len(records) <= 1:
        return records

    import geopandas as gpd
    from scipy.spatial import cKDTree

    gdf = gpd.GeoDataFrame(geometry=[r["centroid"] for r in records], crs=crs)
    try:
        metric = gdf.estimate_utm_crs()
    except Exception:
        metric = crs
    pts = np.array([[g.x, g.y] for g in gdf.to_crs(metric).geometry])

    tree = cKDTree(pts)
    suppressed = np.zeros(len(pts), dtype=bool)
    # highest confidence first: whoever survives removes its neighbours
    for i in np.argsort([-r["confidence"] for r in records]):
        if suppressed[i]:
            continue
        for j in tree.query_ball_point(pts[i], r=distance_m):
            if j != i:
                suppressed[j] = True
    return [r for k, r in enumerate(records) if not suppressed[k]]


# --------------------------------------------------------------------- model
def load_model(path: Path):
    """Load the weights from disk."""
    from rfdetr import RFDETRNano

    if not path.is_file():
        raise SystemExit(f"Model not found: {path}\nRun first:  python download_models.py")
    return RFDETRNano(pretrain_weights=str(path.resolve()))


def resolve_species(species: str) -> tuple[Path, list[str], dict]:
    """Find a species' model in the `models.json` manifest."""
    manifest = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))
    for m in manifest["models"]:
        if m["species"] == species:
            return ROOT / "models" / m["file"], m["classes"], m
    available = ", ".join(m["species"] for m in manifest["models"])
    raise SystemExit(f"Unknown species '{species}'.\nAvailable: {available}")


# ------------------------------------------------------------------- running
def detect(
    orthophoto: Path,
    model_path: Path,
    classes: list[str],
    output: Path,
    tile_px: int = 640,
    overlap: float = 0.2,
    confidence: float = 0.25,
    input_px: int = 384,
    dedup_m: float = 0.5,
    skip_empty: bool = True,
    empty_threshold: float = 12.0,
) -> dict:
    model = load_model(model_path)
    step = max(1, int(tile_px * (1 - overlap)))
    records: list[dict] = []
    t0 = time.perf_counter()

    from shapely.geometry import Point

    with rasterio.open(orthophoto) as src:
        if src.count < 3:
            raise SystemExit(f"The raster needs at least 3 RGB bands (it has {src.count}).")
        transform, crs = src.transform, src.crs
        n_cols = max(1, (src.width - tile_px) // step + 1)
        n_rows = max(1, (src.height - tile_px) // step + 1)
        print(f"[image]  {src.width} x {src.height} px | {crs}")
        print(f"[plan]   {n_rows * n_cols} tiles of {tile_px} px, {int(overlap * 100)}% overlap")

        bar = tqdm(total=n_rows * n_cols, desc="tiles", unit="tile")
        for r in range(n_rows):
            for c in range(n_cols):
                try:
                    col_off, row_off = c * step, r * step
                    data = src.read(window=Window(col_off, row_off, tile_px, tile_px))
                    if data.shape[0] < 3 or data[:3].size == 0:
                        continue
                    real_h, real_w = int(data.shape[1]), int(data.shape[2])
                    rgb = to_rgb_uint8(data[:3])
                    # outside the footprint the orthophoto is black: don't spend the model on it
                    if skip_empty and float(rgb.mean()) < empty_threshold:
                        continue

                    det = model.predict(
                        Image.fromarray(pad(rgb, tile_px, tile_px)),
                        threshold=confidence,
                        shape=(input_px, input_px),
                    )
                    if det is None or len(det) == 0:
                        continue

                    for k in range(len(det.xyxy)):
                        x1, y1, x2, y2 = det.xyxy[k]
                        x1 = min(max(0, x1), max(0, real_w - 1))
                        x2 = min(max(0, x2), real_w)
                        y1 = min(max(0, y1), max(0, real_h - 1))
                        y2 = min(max(0, y2), real_h)
                        if x2 <= x1 or y2 <= y1:
                            continue
                        poly = box_to_polygon(transform, col_off, row_off, x1, y1, x2, y2)
                        if poly.is_empty or not poly.is_valid:
                            continue
                        cid = int(det.class_id[k])
                        records.append({
                            "class": classes[cid] if cid < len(classes) else f"class_{cid}",
                            "class_id": cid,
                            "confidence": float(det.confidence[k]),
                            "box": poly,
                            "centroid": Point(poly.centroid.x, poly.centroid.y),
                        })
                finally:
                    bar.update(1)
        bar.close()

    print(f"[raw]    {len(records)} detections")
    records = deduplicate(records, crs, dedup_m)
    print(f"[dedup]  {len(records)} after removing repeats closer than {dedup_m} m")

    if not records:
        print("No detections - nothing written.")
        return {"n": 0}

    import geopandas as gpd

    attrs = {
        "class": [r["class"] for r in records],
        "class_id": [r["class_id"] for r in records],
        "confidence": [r["confidence"] for r in records],
    }
    boxes = gpd.GeoDataFrame(attrs, geometry=[r["box"] for r in records], crs=crs)
    points = gpd.GeoDataFrame(attrs, geometry=[r["centroid"] for r in records], crs=crs)
    if crs and crs.to_epsg() != 4326:
        boxes, points = boxes.to_crs(4326), points.to_crs(4326)

    output.parent.mkdir(parents=True, exist_ok=True)
    p_boxes = output.with_name(output.stem + "_boxes.geojson")
    p_points = output.with_name(output.stem + "_centroids.geojson")
    boxes.to_file(p_boxes, driver="GeoJSON")
    points.to_file(p_points, driver="GeoJSON")

    csv = points.copy()
    csv["longitude"] = csv.geometry.x
    csv["latitude"] = csv.geometry.y
    csv.drop(columns="geometry").to_csv(output.with_suffix(".csv"), index=False)

    dt = time.perf_counter() - t0
    print(f"\n[done]   {len(records)} plants in {dt / 60:.1f} min")
    for p in (p_boxes, p_points, output.with_suffix(".csv")):
        print(f"         {p.name}")
    return {"n": len(records), "boxes": p_boxes, "centroids": p_points, "seconds": dt}


def main() -> None:
    p = argparse.ArgumentParser(
        description="Run a detection model over a whole orthophoto.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("orthophoto", type=Path)
    p.add_argument("--species", default=None, help="species from the models.json manifest")
    p.add_argument("--model", type=Path, default=None, help="path to the weights")
    p.add_argument("--classes", default=None, help="class names, comma separated")
    p.add_argument("--output", type=Path, default=None)
    p.add_argument("--tile-px", type=int, default=640)
    p.add_argument("--overlap", type=float, default=0.2)
    p.add_argument("--confidence", type=float, default=0.25)
    p.add_argument("--dedup-m", type=float, default=0.5,
                   help="distance below which two detections are the same plant. "
                        "Rule of thumb: half the smallest real spacing between plants.")
    a = p.parse_args()

    if a.species:
        model_path, classes, card = resolve_species(a.species)
        print(f"[model]  {card['title']} - trained at {card['site']}")
        print(f"         precision {card['precision']} · recall {card['recall']} "
              f"(measured there, not here; see 'where_it_fails' in models.json)")
    elif a.model:
        model_path = a.model
        classes = (a.classes or "object").split(",")
    else:
        raise SystemExit("Give --species (from the manifest) or --model and --classes.")

    output = a.output or (ROOT / "outputs" / f"{a.orthophoto.stem}_{a.species or 'model'}")
    detect(
        orthophoto=a.orthophoto, model_path=model_path, classes=classes, output=output,
        tile_px=a.tile_px, overlap=a.overlap, confidence=a.confidence, dedup_m=a.dedup_m,
    )


if __name__ == "__main__":
    main()
