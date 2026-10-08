"""Bulb firmware the Pi keeps for offline updates.

firmware/manifest.json (in the repo) lists each image's model, hardware version,
firmware version, TP-Link URL, size and sha256. fetch() downloads the images
and verifies them into a cache on the Pi; the web server serves that cache so
bulbs can update from the Pi when the venue has no internet.
"""

import hashlib
import json
import os
import urllib.request
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "firmware" / "manifest.json"
DEFAULT_CACHE = Path("/var/lib/dmx_smartbulb/firmware")


def load_manifest(path=MANIFEST):
    return json.loads(Path(path).read_text())["images"]


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def cached(cache=DEFAULT_CACHE, manifest=MANIFEST):
    """Manifest entries whose image is in the cache and verifies."""
    out = []
    for img in load_manifest(manifest):
        p = Path(cache) / img["file"]
        if p.is_file() and p.stat().st_size == img["size"] and sha256_of(p) == img["sha256"]:
            out.append(img)
    return out


def fetch(cache=DEFAULT_CACHE, manifest=MANIFEST, log=print):
    """Download every image not already cached, verifying size and sha256.
    Returns (ok, failed) lists of file names."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    ok, failed = [], []
    for img in load_manifest(manifest):
        dest = cache / img["file"]
        if dest.is_file() and sha256_of(dest) == img["sha256"]:
            log(f"{img['file']}: already cached")
            ok.append(img["file"])
            continue
        tmp = dest.with_name(dest.name + ".part")
        try:
            log(f"{img['file']}: downloading {img['model']} {img['version']}")
            with urllib.request.urlopen(img["url"], timeout=60) as r, open(tmp, "wb") as f:
                while chunk := r.read(65536):
                    f.write(chunk)
            got = sha256_of(tmp)
            if tmp.stat().st_size != img["size"] or got != img["sha256"]:
                raise ValueError(f"checksum mismatch (got {got[:12]}..., expected {img['sha256'][:12]}...)")
            os.replace(tmp, dest)
            log(f"{img['file']}: OK")
            ok.append(img["file"])
        except (OSError, ValueError) as e:
            tmp.unlink(missing_ok=True)
            log(f"{img['file']}: FAILED: {e}")
            failed.append(img["file"])
    return ok, failed


def image_for(model, hw_ver, cache=DEFAULT_CACHE, manifest=MANIFEST):
    """The cached image for exactly this model and hardware version, or None."""
    for img in cached(cache, manifest):
        if img["model"] == model and img["hw_ver"] == hw_ver:
            return img
    return None
