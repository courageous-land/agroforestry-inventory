#!/usr/bin/env python3
"""Convert an orthophoto to a Cloud Optimized GeoTIFF.

A COG is organised in blocks, losslessly compressed, and carries an internal
pyramid of resolutions.

Why that matters: without it, reading a piece of the image forces the program to
walk the whole file. With it, both the detection and the map read only the piece
they need. It is what lets the map be served without generating a pyramid of
image files on disk, which would cost hundreds of megabytes and several minutes
per orthophoto.

The conversion uses the rasterio API and needs no external binary on the PATH, so
it behaves the same on Windows, Linux and Mac.

The input file is NOT modified.

Pyramid depth is computed from the image dimensions rather than fixed: a 4 GB
orthophoto has to go down to 1/128 for the widest zoom to still read from a small
level. Fixed level lists, common in tutorials, work for the image they were
written for and stop working when the image doubles in size.

Examples:
  python ingest.py my_orthophoto.tif
  python ingest.py new_flight.tif --output outputs/new_flight_cog.tif
  python ingest.py input.tif --crop 4096   # small sample, for testing
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from console import utf8

utf8()

import rasterio
from rasterio.shutil import copy as rio_copy
from rasterio.windows import Window

ROOT = Path(__file__).resolve().parent

# Internal COG block size. 512 px is what makes a 256 px map tile always fall
# inside a single block, so each map request reads the least possible from disk.
BLOCK = 512


def overview_levels(width: int, height: int, block: int = BLOCK) -> list[int]:
    """Powers of two until the smallest level fits in one block.

    Same rule the COG driver applies internally; computed here only so the plan
    can be reported and checked after writing.
    """
    levels, factor = [], 2
    while max(width, height) // factor >= block:
        levels.append(factor)
        factor *= 2
    return levels


def human(n_bytes: int) -> str:
    v = float(n_bytes)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or u == "TB":
            return f"{v:.1f} {u}"
        v /= 1024
    return f"{v:.1f} TB"


def crop_centre(source: Path, target: Path, side: int) -> Path:
    """Save a square crop from the centre. Useful for testing ingestion quickly."""
    with rasterio.open(source) as src:
        side = min(side, src.width, src.height)
        col = (src.width - side) // 2
        row = (src.height - side) // 2
        win = Window(col, row, side, side)
        profile = src.profile.copy()
        profile.update(width=side, height=side, transform=src.window_transform(win),
                       compress="deflate", tiled=True, blockxsize=BLOCK, blockysize=BLOCK)
        profile.pop("photometric", None)
        with rasterio.open(target, "w", **profile) as dst:
            dst.write(src.read(window=win))
    print(f"[crop]   {side}x{side} px from the centre -> {target.name} "
          f"({human(target.stat().st_size)})")
    return target


def ingest(source: Path, target: Path, force: bool) -> dict:
    if not source.is_file():
        raise SystemExit(f"Orthophoto not found: {source}")
    if target.exists() and not force:
        raise SystemExit(f"{target} already exists. Use --force to overwrite.")

    with rasterio.open(source) as src:
        width, height, bands = src.width, src.height, src.count
        crs, res = src.crs, src.res
        if bands < 3:
            raise SystemExit(f"The raster needs at least 3 bands (it has {bands}).")
        existing = src.overviews(1)

    levels = overview_levels(width, height)
    print(f"[input]  {source.name}")
    print(f"         {width} x {height} px ({width * height / 1e6:.0f} Mpx), "
          f"{bands} bands, {crs}")
    print(f"         GSD ~ {res[0]:.4f} x {res[1]:.4f} (CRS units)")
    print(f"         {human(source.stat().st_size)}, existing overviews: {existing or 'none'}")
    print(f"[plan]   pyramid down to 1/{levels[-1] if levels else 1} "
          f"(smallest level ~{max(width, height) // (levels[-1] if levels else 1)} px)")

    target.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    # GDAL's COG driver (>= 3.1) writes the file blocked, compressed and with its
    # internal pyramid in a single pass - through the library, without calling any
    # external executable.
    with rasterio.open(source) as src:
        rio_copy(
            src, str(target), driver="COG",
            BLOCKSIZE=BLOCK,
            COMPRESS="DEFLATE",
            PREDICTOR="YES",        # the COG driver picks 2 or 3 by dtype
            LEVEL=6,
            BIGTIFF="YES",          # required above 4 GB, harmless below
            NUM_THREADS="ALL_CPUS",
            OVERVIEW_RESAMPLING="AVERAGE",
        )

    dt = time.perf_counter() - t0
    with rasterio.open(target) as dst:
        ov = dst.overviews(1)
        block_shape = dst.block_shapes[0]
        smallest = max(dst.width, dst.height) // (ov[-1] if ov else 1)

    size = target.stat().st_size
    print(f"[output] {target}")
    print(f"         {human(size)} ({size / source.stat().st_size * 100:.0f}% of the original), "
          f"block {block_shape[0]}x{block_shape[1]}")
    print(f"         overviews {ov} -> smallest level ~{smallest} px")
    print(f"[time]   {dt:.1f} s")
    if not ov:
        print("[WARN]   the COG came out without overviews; wide zoom will read full resolution.")

    return {"file": str(target), "width": width, "height": height, "bands": bands,
            "crs": str(crs), "overviews": ov, "bytes": size, "seconds": round(dt, 1)}


def main() -> None:
    p = argparse.ArgumentParser(
        description="Convert an orthophoto to a COG, ready for detection and for the map.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("input", type=Path, help="input GeoTIFF (not modified)")
    p.add_argument("--output", type=Path, default=None,
                   help="output COG (default: <input>_cog.tif next to the input)")
    p.add_argument("--force", action="store_true", help="overwrite the output if it exists")
    p.add_argument("--crop", type=int, default=0, metavar="PX",
                   help="before converting, take a PX-wide square from the centre (for testing)")
    a = p.parse_args()

    source = a.input if a.input.is_absolute() else (ROOT / a.input)
    target = a.output or source.with_name(f"{source.stem}_cog.tif")
    if not target.is_absolute():
        target = ROOT / target

    if a.crop:
        tmp = target.with_name(f"{target.stem}_raw_crop.tif")
        source = crop_centre(source, tmp, a.crop)

    ingest(source, target, a.force)

    if a.crop and source.name.endswith("_raw_crop.tif"):
        source.unlink(missing_ok=True)

    print(f"\nNext:  python detect.py {target.name} --species <species>")


if __name__ == "__main__":
    main()
