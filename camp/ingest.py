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
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .validate import load_entry, newest_release, validate_listing

LISTING_PATH = ".camp/listing.yml"
MAX_DIMENSION = 1440
MAX_IMAGE_BYTES = 8 * 1024 * 1024


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
