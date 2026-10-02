"""Backfill the declared $plugin->supported range onto release records that
predate the field (camp-tools#64). Reads version.php at each pinned commit
through the GitHub contents API, so no clone is needed; records whose
source is not on GitHub, or whose commit is gone, are left as they are."""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse
from pathlib import Path

import yaml

from .moodleversions import parse_supported_range

_SUPPORTED_RE = re.compile(r"\$plugin->supported\s*=\s*['\"]?([^'\";]+)['\"]?\s*;")


def _github_path(source: str) -> str | None:
    parsed = urllib.parse.urlparse(source or "")
    if parsed.netloc != "github.com":
        return None
    path = parsed.path.strip("/")
    return path[:-4] if path.endswith(".git") else path or None


def fetch_version_php(source: str, commit: str, token=None) -> str | None:
    """version.php text at the commit, or None when the host is not GitHub
    or the file/commit is not there."""
    from .scan import _request
    path = _github_path(source)
    if not path:
        return None
    status, body, _ = _request(
        f"https://api.github.com/repos/{path}/contents/version.php?ref={commit}", token)
    if status != 200:
        return None
    payload = json.loads(body)
    return base64.b64decode(payload.get("content", "")).decode("utf-8", "replace")


def declared_range_in(text: str | None) -> list[int] | None:
    if not text:
        return None
    match = _SUPPORTED_RE.search(text)
    return parse_supported_range(match.group(1)) if match else None


_MATURITY_RE = re.compile(r"\$plugin->maturity\s*=\s*([A-Z_0-9]+)\s*;")


def declared_maturity_in(text: str | None) -> str | None:
    from .maturity import parse_declared
    if not text:
        return None
    match = _MATURITY_RE.search(text)
    return parse_declared(match.group(1)) if match else None


def backfill(index_dir: str | Path, token=None, dry_run: bool = False,
             fetch=fetch_version_php, log=print, field: str = "supported-range") -> dict:
    """Fill `field` ("supported-range" or "maturity", camp-tools#64/#67) on
    release records that lack it, from version.php at each pinned commit."""
    token = token or os.environ.get("GITHUB_TOKEN")
    extract = declared_range_in if field == "supported-range" else declared_maturity_in
    stats = {"written": 0, "requires-only": 0, "already": 0, "unreachable": 0}
    for path in sorted(Path(index_dir).glob("plugins/*/*.yml")):
        entry = yaml.safe_load(path.read_text()) or {}
        releases = entry.get("releases") or []
        if not releases:
            continue
        changed = False
        for release in releases:
            if release.get(field):
                stats["already"] += 1
                continue
            text = fetch(entry.get("source", ""), release["commit"], token)
            if text is None:
                stats["unreachable"] += 1
                log(f"  unreachable: {entry['component']} {release['tag']}")
                continue
            value = extract(text)
            if not value:
                stats["requires-only"] += 1
                continue
            release[field] = value
            stats["written"] += 1
            changed = True
            log(f"  {entry['component']} {release['tag']}: {value}")
        if changed and not dry_run:
            with open(path, "w") as f:
                yaml.safe_dump(entry, f, sort_keys=False, allow_unicode=True)
    return stats
