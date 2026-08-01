"""Listing ingestion (RFC §4.1): pull descriptive content out of the plugin
repository at the released commit, treating it as untrusted input.

For the newest release of an entry:

  1. read `.camp/listing.yml` at the recorded commit (never the branch tip —
     a later repo compromise must not alter a published listing),
  2. if the ledger pins a listing hash, require an exact match,
  3. validate against the listing schema,
  4. re-encode every referenced screenshot: decode, cap dimensions, strip
     all metadata, re-emit as PNG. Raster input only; anything Pillow can't
     decode is dropped with a warning. (Requires Pillow; without it,
     screenshots are skipped entirely rather than copied through unsanitized.)

Output lands in a listings directory consumable by `camp site --listings`.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .validate import load_entry, newest_release, validate_listing

LISTING_PATH = ".camp/listing.yml"
MANIFEST_NAME = "manifest.json"
MAX_DIMENSION = 1440
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_LISTING_BYTES = 1024 * 1024


@dataclass
class IngestResult:
    component: str
    ok: bool
    wrote: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _blob_at(repo: str, commit: str, path: str) -> bytes | None:
    result = subprocess.run(["git", "-C", repo, "show", f"{commit}:{path}"],
                            capture_output=True)
    return result.stdout if result.returncode == 0 else None


def _reencode(data: bytes) -> bytes | None:
    """Decode → bound size → strip metadata → re-emit PNG. Returns None if
    the image can't be safely decoded."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:
        return None
    if max(image.size) > MAX_DIMENSION:
        image.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
    clean = Image.new(image.mode if image.mode in ("RGB", "RGBA", "L") else "RGB",
                      image.size)
    clean.paste(image.convert(clean.mode))
    out = io.BytesIO()
    clean.save(out, format="PNG")
    return out.getvalue()


def ingest_entry(entry_path: str | Path, source: str, out_dir: str | Path) -> IngestResult:
    entry = load_entry(entry_path)
    component = entry["component"]
    result = IngestResult(component=component, ok=True)

    if not entry["releases"]:
        result.problems.append("no releases; nothing to ingest (tier 0/1)")
        result.ok = False
        return result
    release = newest_release(entry)
    commit = release["commit"]

    raw = _blob_at(source, commit, LISTING_PATH)
    if raw is None:
        result.problems.append(f"no {LISTING_PATH} at {commit[:12]}")
        result.ok = False
        return result

    pinned = release.get("listing-sha256")
    actual = hashlib.sha256(raw).hexdigest()
    if pinned and actual != pinned:
        result.problems.append(
            f"listing at {commit[:12]} has sha256 {actual[:16]}…, ledger pins {pinned[:16]}…")
        result.ok = False
        return result
    if not pinned:
        result.warnings.append("ledger does not pin a listing hash for this release")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    listing_out = out / f"{component}.yml"
    listing_out.write_bytes(raw)

    schema_problems = validate_listing(listing_out)
    if schema_problems:
        listing_out.unlink()
        result.problems.extend(f"listing invalid: {p}" for p in schema_problems)
        result.ok = False
        return result
    result.wrote.append(str(listing_out))

    listing = yaml.safe_load(raw)
    screenshots = listing.get("screenshots") or []
    if screenshots:
        try:
            import PIL  # noqa: F401
        except ImportError:
            result.warnings.append(
                f"{len(screenshots)} screenshot(s) skipped: Pillow not installed "
                "(images are never copied through unsanitized)")
            screenshots = []
    for shot in screenshots:
        path = shot["path"]
        blob = _blob_at(source, commit, path)
        if blob is None:
            result.warnings.append(f"screenshot missing at {commit[:12]}: {path}")
            continue
        if len(blob) > MAX_IMAGE_BYTES:
            result.warnings.append(f"screenshot too large, dropped: {path}")
            continue
        clean = _reencode(blob)
        if clean is None:
            result.warnings.append(f"screenshot could not be decoded, dropped: {path}")
            continue
        shot_out = out / "screenshots" / component / (Path(path).stem + ".png")
        shot_out.parent.mkdir(parents=True, exist_ok=True)
        shot_out.write_bytes(clean)
        result.wrote.append(str(shot_out))

    return result


# --------------------------------------------------------------- publish ----

def _fetch_bytes(base: str, rel: str, cap: int) -> bytes | None:
    try:
        if base.startswith("https://"):
            req = urllib.request.Request(
                f"{base}/{rel}", headers={"User-Agent": "camp-ingest-reuse"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read(cap)
        return Path(base, rel).read_bytes()[:cap]
    except Exception:
        return None


def _reuse_entry(component: str, release: dict, prior: dict, reuse: str,
                 out: Path, log) -> bool:
    """Refetch this entry's published listing + screenshots instead of
    cloning. All-or-nothing: any miss falls back to a normal ingest. The
    ledger's pinned listing hash is re-checked, so reuse can't serve bytes
    the ledger doesn't vouch for."""
    raw = _fetch_bytes(reuse, f"listings/{component}.yml", MAX_LISTING_BYTES)
    if raw is None:
        return False
    pinned = release.get("listing-sha256")
    if pinned and hashlib.sha256(raw).hexdigest() != pinned:
        log(f"ingest: {component}: published listing no longer matches the "
            "ledger pin; re-ingesting from source")
        return False
    listing_out = out / f"{component}.yml"
    listing_out.write_bytes(raw)
    if validate_listing(listing_out):
        listing_out.unlink()
        return False
    shots_dir = out / "screenshots" / component
    for name in prior.get("shots") or []:
        name = Path(name).name             # no traversal from a hostile manifest
        if not name.endswith(".png"):
            continue
        blob = _fetch_bytes(reuse, f"shots/{component}/{name}", MAX_IMAGE_BYTES)
        if blob is None:
            listing_out.unlink()
            if shots_dir.exists():
                shutil.rmtree(shots_dir)
            return False
        shots_dir.mkdir(parents=True, exist_ok=True)
        (shots_dir / name).write_bytes(blob)
    return True


def ingest_all(index_dir: str | Path, out_dir: str | Path,
               reuse: str | None = None, log=print) -> tuple[int, int]:
    """Ingest every released entry, cloning each source once. With `reuse`
    (the live site's base URL, or a previous dist dir), entries whose
    newest-release commit matches the previous publish's manifest are
    refetched from the published site instead of cloned — publish cost
    becomes O(new releases), not O(all releases ever).

    Writes `manifest.json` beside the listings so the *next* publish can
    reuse this one. Returns (ingested, reused)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    prior: dict = {}
    if reuse:
        raw = _fetch_bytes(reuse, f"listings/{MANIFEST_NAME}", 8 * 1024 * 1024)
        if raw:
            try:
                prior = (json.loads(raw) or {}).get("components") or {}
            except ValueError:
                prior = {}
    manifest: dict = {}
    ingested = reused = 0
    for entry_path in sorted(Path(index_dir).glob("plugins/*/*.yml")):
        entry = load_entry(entry_path)
        if not entry.get("releases") or entry.get("status") == "delisted":
            continue
        component = entry["component"]
        release = newest_release(entry)
        pm = prior.get(component)
        if pm and pm.get("commit") == release["commit"] and pm.get("no-listing"):
            # negative cache: this commit had no listing manifest last time
            # and commits are immutable — nothing to clone for
            manifest[component] = {"commit": release["commit"], "no-listing": True}
            reused += 1
            continue
        if (pm and pm.get("commit") == release["commit"] and reuse
                and _reuse_entry(component, release, pm, reuse, out, log)):
            reused += 1
        else:
            with tempfile.TemporaryDirectory(prefix="camp-ingest-") as td:
                src = str(Path(td) / "src")
                clone = subprocess.run(
                    ["git", "clone", "--quiet", entry["source"], src],
                    capture_output=True)
                if clone.returncode != 0:
                    log(f"ingest: {component}: clone failed: "
                        f"{clone.stderr.decode(errors='replace').strip()}")
                    continue
                result = ingest_entry(entry_path, src, out)
            for warning in result.warnings:
                log(f"ingest: {component}: {warning}")
            if not result.ok:
                for problem in result.problems:
                    log(f"ingest: {component}: {problem}")
                if any("no .camp/listing.yml" in p for p in result.problems):
                    # benign and commit-stable: cache the absence so the next
                    # publish skips the clone. Hard failures (pin mismatch,
                    # invalid listing) are NOT cached — they must re-surface.
                    manifest[component] = {"commit": release["commit"],
                                           "no-listing": True}
                continue
            ingested += 1
        listing_path = out / f"{component}.yml"
        shots_dir = out / "screenshots" / component
        manifest[component] = {
            "commit": release["commit"],
            "listing-sha256": hashlib.sha256(listing_path.read_bytes()).hexdigest(),
            "shots": sorted(p.name for p in shots_dir.glob("*.png"))
                     if shots_dir.exists() else [],
        }
    (out / MANIFEST_NAME).write_text(
        json.dumps({"schema": 1, "components": manifest}, sort_keys=True) + "\n")
    return ingested, reused
