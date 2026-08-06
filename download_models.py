#!/usr/bin/env python3
"""Download the model weights and verify each file.

The weights are not kept in the repository: 121 MB each, and git is no place for
large binaries. They are published as release assets and fetched here.

Every file is checked against the sha256 declared in `models.json`. A download cut
short produces a truncated file that loads and infers without complaining - which
is why the check is not optional, and a file only counts as valid once verified.

Usage:
    python download_models.py                     # all of them
    python download_models.py --species coffee    # just one
    python download_models.py --from /local/path  # offline, copy from a folder
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from console import utf8
from network import prefer_ipv4

utf8()
if prefer_ipv4():
    print("[network] IPv6 advertised but not routable; using IPv4")

ROOT = Path(__file__).resolve().parent
TARGET_DIR = ROOT / "models"
CHUNK = 1 << 20  # 1 MB


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def verified(path: Path, expected: str) -> bool:
    return path.is_file() and sha256(path) == expected


def bar(done: int, total: int, width: int = 34) -> str:
    if not total:
        return f"{done / 1e6:.0f} MB"
    full = int(width * done / total)
    return f"[{'=' * full}{' ' * (width - full)}] {100 * done / total:5.1f}%"


def download(url: str, target: Path) -> None:
    partial = target.with_suffix(target.suffix + ".partial")
    try:
        with urllib.request.urlopen(url) as response, partial.open("wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while chunk := response.read(CHUNK):
                out.write(chunk)
                done += len(chunk)
                print(f"\r    {bar(done, total)}", end="", flush=True)
        print()
    except urllib.error.URLError as e:
        partial.unlink(missing_ok=True)
        raise SystemExit(
            f"\nCould not download {url}\n  {e}\n"
            "If you are offline, use:  python download_models.py --from <folder with the .pth>"
        ) from e
    partial.replace(target)


def main() -> None:
    p = argparse.ArgumentParser(description="Download and verify the model weights.")
    p.add_argument("--species", default=None, help="download only this species")
    p.add_argument("--from", dest="source", type=Path, default=None,
                   help="copy from a local folder instead of downloading (offline use)")
    p.add_argument("--force", action="store_true", help="re-download even if the file verifies")
    a = p.parse_args()

    manifest = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))
    base_url = manifest.get("base_url")
    models = manifest["models"]
    if a.species:
        models = [m for m in models if m["species"] == a.species]
        if not models:
            raise SystemExit(f"Unknown species '{a.species}'.")

    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    ok, missing = 0, []

    for m in models:
        target = TARGET_DIR / m["file"]
        print(f"\n{m['title']}  ({m['bytes'] / 1e6:.0f} MB)")

        if not a.force and verified(target, m["sha256"]):
            print("    already here and verified")
            ok += 1
            continue

        if a.source:
            origin = a.source / m["file"]
            if not origin.is_file():
                print(f"    not found in {a.source}")
                missing.append(m["species"])
                continue
            print(f"    copying from {origin}")
            shutil.copy2(origin, target)
        elif base_url:
            download(base_url.rstrip("/") + "/" + m["file"], target)
        else:
            print("    [pending] the manifest has no 'base_url' yet - the weights have not")
            print("              been published. Use --from <folder> meanwhile.")
            missing.append(m["species"])
            continue

        if verified(target, m["sha256"]):
            print("    verified")
            ok += 1
        else:
            target.unlink(missing_ok=True)
            print("    CORRUPTED FILE (sha256 mismatch) - removed")
            missing.append(m["species"])

    print(f"\n{ok} of {len(models)} model(s) ready in {TARGET_DIR}")
    if missing:
        print(f"Missing: {', '.join(missing)}")
        raise SystemExit(1)
    print("\nNext:  python inventory.py example/coffee_raizes.tif --species coffee")


if __name__ == "__main__":
    main()
