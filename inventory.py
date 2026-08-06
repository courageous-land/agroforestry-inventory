#!/usr/bin/env python3
"""From orthophoto to map, in one command.

    python inventory.py my_orthophoto.tif --species coffee

Chains the three stages and opens the browser at the end:

    1. ingest    GeoTIFF -> COG (once per orthophoto; reused if already there)
    2. detect    COG -> GeoJSON, one feature per plant
    3. map       local server showing the orthophoto and the detections

Each stage also runs on its own, if you would rather control the parameters:

    python ingest.py  my_orthophoto.tif
    python detect.py  my_orthophoto_cog.tif --species coffee
    python server.py  --orthophoto ... --detections ...

Runs without a GPU and without internet (once the models are downloaded).
Nothing is sent outside this machine.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from console import utf8

utf8()

ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"


def stage(n: int, total: int, title: str) -> None:
    print(f"\n\033[1m[{n}/{total}] {title}\033[0m")


def is_cog(path: Path) -> bool:
    """Good enough here means tiled, with overviews. Avoids reconverting needlessly."""
    import rasterio

    try:
        with rasterio.open(path) as src:
            return bool(src.overviews(1)) and src.profile.get("tiled", False)
    except Exception:
        return False


def main() -> None:
    p = argparse.ArgumentParser(
        description="From orthophoto to a map of detections, in one command.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("orthophoto", type=Path, help="orthophoto GeoTIFF")
    p.add_argument("--species", required=True, help="see the list in models.json")
    p.add_argument("--confidence", type=float, default=0.25)
    p.add_argument("--dedup-m", type=float, default=None,
                   help="default: the value recommended for the species in models.json")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--redo", action="store_true", help="ignore previous results")
    a = p.parse_args()

    source = a.orthophoto if a.orthophoto.is_absolute() else (Path.cwd() / a.orthophoto)
    if not source.is_file():
        raise SystemExit(f"Orthophoto not found: {source}")

    from detect import detect, resolve_species

    model_path, classes, card = resolve_species(a.species)
    if not model_path.is_file():
        raise SystemExit(
            f"Weights for '{a.species}' are not in {model_path.parent}.\n"
            "Run first:  python download_models.py"
        )

    OUTPUTS.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    print(f"\n  \033[1m{card['title']}\033[0m")
    print(f"  trained at {card['site']} - precision {card['precision']}, "
          f"recall {card['recall']} \033[2m(measured there, not here)\033[0m")

    stage(1, 3, "Preparing the orthophoto")
    if is_cog(source):
        cog = source
        print(f"  already in a suitable format: {cog.name}")
    else:
        cog = OUTPUTS / f"{source.stem}_cog.tif"
        if cog.is_file() and not a.redo:
            print(f"  reusing {cog.name}")
        else:
            from ingest import ingest

            ingest(source, cog, force=True)

    stage(2, 3, "Looking for plants")
    base = OUTPUTS / f"{source.stem}_{a.species}"
    boxes = base.with_name(base.stem + "_boxes.geojson")
    if boxes.is_file() and not a.redo:
        import json

        n = len(json.loads(boxes.read_text(encoding="utf-8"))["features"])
        print(f"  reusing {boxes.name} ({n} detections)")
        print("  use --redo to run it again")
    else:
        r = detect(
            orthophoto=cog, model_path=model_path, classes=classes, output=base,
            confidence=a.confidence,
            dedup_m=a.dedup_m if a.dedup_m is not None else card.get("recommended_dedup_m", 0.5),
        )
        if not r.get("n"):
            print("\n  No plants found. That can mean two things:")
            print("  - the species is not in this orthophoto; or")
            print("  - this model does not suit your imagery, which is common and expected.")
            print(f"    See 'where_it_fails' for '{a.species}' in models.json.")
            boxes = None

    stage(3, 3, "Opening the map")
    print(f"  total time so far: {(time.perf_counter() - t0) / 60:.1f} min")

    from server import serve

    serve(cog, boxes, port=a.port, open_browser=not a.no_browser, card=card)


if __name__ == "__main__":
    main()
