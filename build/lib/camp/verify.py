"""Source verification: prove each recorded release matches its public source.

For every release in an index entry (RFC §4.2):

  1. clone the declared source repository (or use a local checkout),
  2. resolve the declared tag and confirm it still points at the recorded
     commit — a moved or force-pushed tag is a hard failure,
  3. rebuild the canonical ZIP deterministically from that commit,
  4. confirm the rebuilt artifact's SHA-256 matches the ledger,
  5. if a listing hash is pinned, confirm .camp/listing.yml at that commit
     still matches it.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .build import BuildError, build_zip, file_sha256_at_commit, resolve_tag
from .validate import load_entry

LISTING_PATH = ".camp/listing.yml"


@dataclass
class ReleaseResult:
    version: str
    ok: bool
    checks: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _clone(source: str, dest: str) -> None:
    result = subprocess.run(
        ["git", "clone", "--quiet", source, dest],
        capture_output=True,
    )
    if result.returncode != 0:
        raise BuildError(f"clone of {source} failed: {result.stderr.decode(errors='replace').strip()}")


def verify_entry(entry_path: str | Path, source_override: str | None = None) -> list[ReleaseResult]:
    """Verify every release of one index entry. source_override uses a local
    checkout instead of cloning (for development and for CI cache hits)."""
    entry = load_entry(entry_path)
    component = entry["component"]
    results: list[ReleaseResult] = []

    with tempfile.TemporaryDirectory(prefix="camp-verify-") as tmp:
        if source_override:
            repo = source_override
        else:
            repo = str(Path(tmp) / "source")
            _clone(entry["source"], repo)

        for release in entry["releases"]:
            result = ReleaseResult(version=release["version"], ok=True)
            results.append(result)

            try:
                commit = resolve_tag(repo, release["tag"])
            except BuildError as exc:
                result.ok = False
                result.problems.append(f"tag {release['tag']}: {exc}")
                continue

            if commit != release["commit"]:
                result.ok = False
                result.problems.append(
                    f"tag {release['tag']} now points at {commit[:12]}, ledger records "
                    f"{release['commit'][:12]} — tag was moved after publication"
                )
                continue
            result.checks.append(f"tag {release['tag']} -> {commit[:12]} matches ledger")

            try:
                artifact = build_zip(repo, release["tag"], component)
            except BuildError as exc:
                result.ok = False
                result.problems.append(f"build failed: {exc}")
                continue

            if artifact.sha256 != release["zip-sha256"]:
                result.ok = False
                result.problems.append(
                    f"rebuilt ZIP sha256 {artifact.sha256} != ledger {release['zip-sha256']}"
                )
            else:
                result.checks.append(
                    f"rebuilt ZIP ({artifact.file_count} files) sha256 matches ledger"
                )

            pinned = release.get("listing-sha256")
            if pinned:
                actual = file_sha256_at_commit(repo, commit, LISTING_PATH)
                if actual is None:
                    result.ok = False
                    result.problems.append(f"{LISTING_PATH} missing at {commit[:12]} but hash is pinned")
                elif actual != pinned:
                    result.ok = False
                    result.problems.append(f"{LISTING_PATH} hash mismatch at {commit[:12]}")
                else:
                    result.checks.append("pinned listing manifest matches")

    return results
