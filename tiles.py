"""Map tiles cut from the COG on demand.

This removes the image pyramid you would normally generate before showing an
orthophoto on the web - which for a 1.9 GB image costs 608 MB on disk and several
minutes of processing, and for a 4 GB one goes past 1.3 GB.

The cost here is O(tile), not O(image): the crop is read from the overview level
closest to the requested resolution, so the widest zoom - which covers the most
ground - is the cheapest, not the most expensive. Measured on a real orthophoto:
23-67 ms per tile from z16 to z23, and 2 ms once cached. On a 4 GB orthophoto the
number is the same, as long as the pyramid goes deep enough (which is what
`ingest.py` guarantees).

Two traps handled here:

  * **Threads.** The server is a ThreadingHTTPServer. A rasterio DatasetReader
    cannot be read by two threads at once - the result is a corrupted tile or a
    crash. Each thread gets its own reader (`threading.local`).

  * **Axis convention.** There are two ways to number tiles, XYZ and TMS, which
    differ in the direction of the Y axis: `y_tms = 2**z - 1 - y_xyz`. Swapping
    one for the other mirrors the map vertically without raising any error. The
    caller says which one it wants; the viewer asks for TMS.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from rasterio.crs import CRS
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import Reader

_EMPTY: bytes | None = None


def empty_png() -> bytes:
    """A fully transparent 256x256 RGBA PNG.

    Built with PIL on purpose. An earlier version used rio-tiler's `ImageData`
    with a zeroed mask, expecting "0 means transparent" - but the mask convention
    changed between versions, and the result was an **opaque black** tile, which
    would have painted the whole area around the orthophoto black. Here all four
    channels are written explicitly and there is no convention to get wrong.
    """
    global _EMPTY
    if _EMPTY is None:
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(buf, format="PNG")
        _EMPTY = buf.getvalue()
    return _EMPTY


def y_to_xyz(z: int, y: int) -> int:
    """Convert a TMS Y index to XYZ (the operation is its own inverse)."""
    return (1 << z) - 1 - y


class Pyramid:
    """One COG served as a tile pyramid, with an on-disk cache.

    The cache is optional but worth having: the first visit to an area pays the
    ~20 ms read, later ones read from disk. Its key includes the raster's size and
    mtime, so re-ingesting the orthophoto invalidates it automatically.
    """

    def __init__(self, path: Path, cache_dir: Path | None = None,
                 resampling: str = "cubic") -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"COG not found: {self.path}")

        # `cubic` was chosen by measurement: comparing 40 tiles between z18 and
        # z21 against a pyramid generated the classic way, it differed least
        # (mean difference 7.97/255, against 9.55 for `nearest`, the library
        # default). Alignment is exact either way; what remains is resampling
        # difference, invisible to the eye.
        self.resampling = resampling
        self._local = threading.local()

        st = self.path.stat()
        key = f"{self.path.resolve()}|{st.st_size}|{int(st.st_mtime)}"
        self.signature = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        self.cache_dir = (cache_dir / self.signature) if cache_dir else None

        # bounds and zoom range come from the file itself; the viewer needs them
        # or it will request tiles for the whole world
        with Reader(str(self.path)) as r:
            self.minzoom = int(r.minzoom)
            self.maxzoom = int(r.maxzoom)
            self.bounds_4326 = tuple(
                round(v, 8) for v in r.get_geographic_bounds(CRS.from_epsg(4326))
            )

    @property
    def _reader(self) -> Reader:
        r = getattr(self._local, "reader", None)
        if r is None:
            r = Reader(str(self.path))
            self._local.reader = r
        return r

    def close(self) -> None:
        r = getattr(self._local, "reader", None)
        if r is not None:
            r.close()
            self._local.reader = None

    def _cache_path(self, z: int, x: int, y: int) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / str(z) / str(x) / f"{y}.png"

    def tile(self, z: int, x: int, y: int, scheme: str = "xyz") -> bytes:
        """PNG for one tile. Outside the footprint returns a transparent PNG, not an error."""
        if scheme == "tms":
            y = y_to_xyz(z, y)

        cached = self._cache_path(z, x, y)
        if cached and cached.is_file():
            return cached.read_bytes()

        try:
            img = self._reader.tile(x, y, z, resampling_method=self.resampling)
        except TileOutsideBounds:
            return empty_png()

        png = img.render(img_format="PNG")
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            tmp = cached.with_suffix(".tmp")
            tmp.write_bytes(png)
            tmp.replace(cached)   # atomic: two threads may ask for the same tile
        return png

    def info(self) -> dict:
        return {
            "file": self.path.name,
            "minzoom": self.minzoom,
            "maxzoom": self.maxzoom,
            "bounds_4326": list(self.bounds_4326),
            "resampling": self.resampling,
            "cache": str(self.cache_dir) if self.cache_dir else None,
        }


class Pyramids:
    """Cache of `Pyramid` objects by raster path.

    Several views may point at the same orthophoto; opening it once avoids
    re-reading the metadata and multiplying readers.
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir
        self._by_path: dict[str, Pyramid] = {}
        self._lock = threading.Lock()

    def get(self, path: Path) -> Pyramid:
        key = str(Path(path).resolve())
        with self._lock:
            p = self._by_path.get(key)
            if p is None:
                p = Pyramid(Path(path), self.cache_dir)
                self._by_path[key] = p
            return p
