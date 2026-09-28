#!/usr/bin/env python3
"""Build a static version of the viewer - no server, just files.

Useful for hosting the demo anywhere that serves HTML: GitHub Pages, a static
Hugging Face Space, a bucket. No Python needed on the other side.

The normal mode cuts each tile from the COG on request, which is right for a
multi-gigabyte orthophoto: pre-generating the whole pyramid would cost hundreds
of megabytes. Over a small demo area the arithmetic flips - a few dozen tiles,
and pre-generating them removes the server entirely.

The viewer is the same in both cases: it tries the API and, finding none, reads
the files next to the page.

Usage:
    python build_static.py --orthophoto example/coffee_raizes.tif         --detections outputs/coffee_raizes_coffee_boxes.geojson         --species coffee --output static/
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
from pathlib import Path

from console import utf8

utf8()

ROOT = Path(__file__).resolve().parent


def build(orthophoto: Path, detections: Path, species: str | None, output: Path) -> dict:
    from tiles import Pyramid

    output.mkdir(parents=True, exist_ok=True)
    pyr = Pyramid(orthophoto, cache_dir=None)
    info = pyr.info()
    west, south, east, north = info["bounds_4326"]

    # copy the viewer as it is - one codebase for both modes
    shutil.copytree(ROOT / "web", output, dirs_exist_ok=True)
    shutil.copy2(detections, output / "detections.geojson")

    card = None
    if species:
        manifest = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))
        card = next((m for m in manifest["models"] if m["species"] == species), None)

    n_tiles, total_bytes = 0, 0
    for z in range(info["minzoom"], info["maxzoom"] + 1):
        n = 1 << z

        def x_of(lon):
            return int((lon + 180.0) / 360.0 * n)

        def y_tms_of(lat):
            r = math.radians(lat)
            y_xyz = int((1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * n)
            return n - 1 - y_xyz

        x0, x1 = x_of(west), x_of(east)
        y0, y1 = sorted((y_tms_of(south), y_tms_of(north)))

        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                png = pyr.tile(z, x, y, scheme="tms")
                if len(png) < 900:      # transparent tile: outside the footprint
                    continue
                target = output / "tiles" / str(z) / str(x) / f"{y}.png"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(png)
                n_tiles += 1
                total_bytes += len(png)
        print(f"  z{z}: {(x1 - x0 + 1)} x {(y1 - y0 + 1)} tiles considered")

    n_det = len(json.loads(detections.read_text(encoding="utf-8"))["features"])

    (output / "info.json").write_text(
        json.dumps({**info, "scheme": "tms", "tiles_url": "./tiles/{z}/{x}/{y}.png",
                    "n_detections": n_det, "model": card}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    print(f"\n  {n_tiles} tiles written ({total_bytes / 1e6:.1f} MB)")
    print(f"  {n_det} detections")
    print(f"  output: {output}")
    return {"tiles": n_tiles, "bytes": total_bytes, "detections": n_det}


def main() -> None:
    p = argparse.ArgumentParser(description="Build the viewer as static files.")
    p.add_argument("--orthophoto", type=Path, required=True)
    p.add_argument("--detections", type=Path, required=True)
    p.add_argument("--species", default=None, help="to include the model card")
    p.add_argument("--output", type=Path, default=ROOT / "static")
    a = p.parse_args()
    for f in (a.orthophoto, a.detections):
        if not f.is_file():
            raise SystemExit(f"Not found: {f}")
    build(a.orthophoto, a.detections, a.species, a.output)


if __name__ == "__main__":
    main()
