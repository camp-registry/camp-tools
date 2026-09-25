"""TUF targets for what clients actually install (phase 2, camp-tools#47).

Two trees are published (DESIGN D22): the site tree (GitHub Pages: the
browse website, packages.json, the advisory feed, index.json) and the
artifact tree (the ZIPs, on B2 behind artifacts.camp-registry.org). The
signed targets cover the machine-readable files clients fetch, not the
website's 6,000 HTML pages:

- SITE_TARGETS: the few site-tree files, hashed from disk at publish;
- ledger_targets(): every ledger release that installation metadata
  serves — the same set composer.generate emits (tier >= 2, not delisted,
  not revoked by advisory), named by artifact_relpath so the target name
  is the path under the artifact host. The sha256 is the ledger's (the
  value the registry verified by rebuilding the tag); the byte length,
  which TUF also signs, comes from the archive audit's HEAD of the stored
  object (`camp archive-audit --lengths-out`).
"""

from __future__ import annotations

from pathlib import Path

from tuf.api.metadata import TargetFile

from .advisory import AdvisorySet
from .artifacts import artifact_relpath
from .validate import load_entry

# Site-tree files a client reads to decide what to install. Relative to
# the dist root; hashed from disk by sign_repository(only=...).
SITE_TARGETS = ["packages.json", "security-advisories.json", "index.json"]


def served_releases(index_dir: str | Path):
    """(entry, release, artifact path) for every release installation
    metadata serves; mirrors composer.generate's exclusions exactly."""
    root = Path(index_dir)
    advisories = AdvisorySet.load(root) if (root / "advisories").is_dir() else None
    for entry_path in sorted(root.glob("plugins/*/*.yml")):
        entry = load_entry(entry_path)
        if entry.get("status", "active") == "delisted" or entry.get("tier", 0) < 2:
            continue
        component = entry["component"]
        for release in entry.get("releases") or []:
            version = str(release["version"]).split(" ")[0]
            if advisories is not None and advisories.is_revoked(component, version):
                continue
            yield entry, release, artifact_relpath(component, release)


def ledger_targets(index_dir: str | Path,
                   lengths: dict[str, int]) -> tuple[dict[str, TargetFile], list[str]]:
    """Target files for the served releases. Returns (targets, problems);
    a release whose length is unknown is a problem, not a silent gap —
    publishing metadata that omits a served release would let a client
    refuse a perfectly good install."""
    targets: dict[str, TargetFile] = {}
    problems: list[str] = []
    for entry, release, name in served_releases(index_dir):
        length = lengths.get(name)
        if length is None:
            problems.append(f"{name}: no artifact length (run `camp archive-audit "
                            f"--lengths-out` in the same publish)")
            continue
        targets[name] = TargetFile.from_dict(
            {"length": int(length), "hashes": {"sha256": release["zip-sha256"]}},
            name)
    return targets, problems
