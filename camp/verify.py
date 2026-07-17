"""Source verification: prove each recorded release matches its public source.

For every release in an index entry (RFC §4.2):

  1. clone the declared source repository (or use a local checkout),
  2. resolve the declared tag and confirm it still points at the recorded
     commit — a moved or force-pushed tag is a hard failure,
  3. rebuild the canonical ZIP deterministically from that commit,
  4. confirm the rebuilt artifact's SHA-256 matches the ledger,
  5. if a listing hash is pinned, confirm .camp/listing.yml at that commit
     still matches it,
  6. confirm every location declared in the release's thirdpartylibs.xml
     exists in the artifact — the tag must contain everything it declares
     (AUTHORS.md release rule three; Moodle's own grunt tooling stats each
     declared location in every installed component and aborts the whole
     site build on a missing one).
"""

from __future__ import annotations

import io
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from .build import BuildError, build_zip, file_sha256_at_commit, plugin_folder, resolve_tag
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


def thirdparty_problems(zip_data: bytes, component: str) -> list[str] | None:
    """Declared-contents check for one built artifact.

    Returns None when the release ships no thirdpartylibs.xml, else the
    list of problems: declared locations absent from the artifact, or a
    manifest that isn't valid XML. Both fail verification — a release
    whose own manifest misdescribes it can't be vouched for, and Moodle's
    grunt ignorefiles hard-fails any site that installs it."""
    folder = plugin_folder(component)
    with zipfile.ZipFile(io.BytesIO(zip_data)) as archive:
        names = set(archive.namelist())
        manifest = f"{folder}/thirdpartylibs.xml"
        if manifest not in names:
            return None
        raw = archive.read(manifest)
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        return [f"thirdpartylibs.xml is not well-formed XML: {exc}"]
    problems = []
    for library in root.iter("library"):
        location = (library.findtext("location") or "").strip().strip("/")
        if not location:
            continue
        prefixed = f"{folder}/{location}"
        if prefixed in names or any(n.startswith(prefixed + "/") for n in names):
            continue
        problems.append(
            f"thirdpartylibs.xml declares {location}, which is not in the "
            f"release — the tag must contain everything it declares"
        )
    return problems


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

            declared = thirdparty_problems(artifact.data, component)
            if declared:
                result.ok = False
                result.problems.extend(declared)
            elif declared is not None:
                result.checks.append("thirdpartylibs.xml declared locations all present")

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
