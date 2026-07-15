"""Materialize the artifact tree (RFC §4.5): every ledger release's ZIP.

Publishing is re-verification: each artifact is rebuilt deterministically
from its tagged source and refuses to ship unless its SHA-256 matches the
ledger — the publisher cannot serve a ZIP the index doesn't vouch for.
Revoked versions are withdrawn from the distribution tree (the ledger and
archive keep history, RFC §5.3); already-materialized artifacts whose
hashes still match are kept without rebuilding, so repeated publishes only
build what's new.
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .advisory import AdvisorySet
from .build import BuildError, build_zip
from .validate import load_entry
from .verify import _clone


@dataclass
class MaterializeResult:
    built: int = 0
    kept: int = 0
    withdrawn: int = 0
    problems: list[str] = field(default_factory=list)


def artifact_relpath(component: str, release: dict) -> str:
    """Relative path of a release's ZIP inside the artifact tree.

    Must match the dist URLs in the Composer metadata (composer.py): the
    version is the release string's first whitespace-separated token.
    """
    version = release["version"].split(" ")[0]
    return f"{component}/{component}-{version}.zip"


def materialize(index_dir: str | Path, out_dir: str | Path) -> MaterializeResult:
    """Build the artifact tree for every non-revoked ledger release."""
    out = Path(out_dir)
    advisories = AdvisorySet.load(index_dir)
    result = MaterializeResult()

    for entry_path in sorted(Path(index_dir).glob("plugins/*/*.yml")):
        entry = load_entry(entry_path)
        component = entry["component"]
        if not entry["releases"] or entry.get("status") == "delisted":
            continue

        pending = []
        for release in entry["releases"]:
            version = release["version"].split(" ")[0]
            if advisories.is_revoked(component, version):
                result.withdrawn += 1
                continue
            dest = out / artifact_relpath(component, release)
            if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == release["zip-sha256"]:
                result.kept += 1
                continue
            pending.append((release, dest))
        if not pending:
            continue

        with tempfile.TemporaryDirectory(prefix="camp-artifacts-") as tmp:
            repo = str(Path(tmp) / "source")
            try:
                _clone(entry["source"], repo)
            except BuildError as exc:
                result.problems.append(f"{component}: {exc}")
                continue
            for release, dest in pending:
                try:
                    artifact = build_zip(repo, release["tag"], component)
                except BuildError as exc:
                    result.problems.append(f"{component} {release['version']}: {exc}")
                    continue
                if artifact.sha256 != release["zip-sha256"]:
                    result.problems.append(
                        f"{component} {release['version']}: rebuilt sha256 "
                        f"{artifact.sha256} != ledger {release['zip-sha256']} — not shipped"
                    )
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(artifact.data)
                result.built += 1

    return result
