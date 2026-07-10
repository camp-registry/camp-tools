"""Deterministic distribution ZIP builder (RFC §4.2, "reproducible build").

The canonical artifact for a release is defined by this module, not by any
particular git version's `git archive --format=zip` output. We take the file
*set and contents* from `git archive --format=tar` (which honours the
author's .gitattributes export-ignore rules), then re-pack them into a
normalized ZIP:

  - entries sorted by path
  - a single top-level folder named after the plugin (Moodle install layout)
  - every timestamp fixed to the tagged commit's committer time (UTC)
  - permissions normalized to 0644, or 0755 when the tar entry is executable
  - deflate compression at a fixed level, no ZIP extra fields

Anyone can independently rebuild the artifact from the public tag and get a
byte-identical file, regardless of their git or zip version.
"""

from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
import time
import zipfile
from dataclasses import dataclass


class BuildError(Exception):
    pass


def _git(repo: str, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
    )
    if result.returncode != 0:
        raise BuildError(
            f"git {' '.join(args)} failed in {repo}: {result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout


def plugin_folder(component: str) -> str:
    """Moodle install folder for a component: the part after the plugin type.

    mod_cmi5launch -> cmi5launch, local_ai_manager -> ai_manager.
    """
    _, _, name = component.partition("_")
    if not name:
        raise BuildError(f"not a frankenstyle component name: {component}")
    return name


def resolve_tag(repo: str, tag: str) -> str:
    """Commit SHA a tag points to (peeled through annotated tags)."""
    return _git(repo, "rev-parse", f"{tag}^{{commit}}").decode().strip()


def commit_timestamp(repo: str, commit: str) -> int:
    return int(_git(repo, "log", "-1", "--format=%ct", commit).decode().strip())


@dataclass
class BuiltArtifact:
    data: bytes
    sha256: str
    commit: str
    file_count: int


def build_zip(repo: str, tag: str, component: str) -> BuiltArtifact:
    """Build the canonical distribution ZIP for `component` at `tag`."""
    commit = resolve_tag(repo, tag)
    ts = commit_timestamp(repo, commit)
    date_time = time.gmtime(ts)[:6]
    folder = plugin_folder(component)

    tar_bytes = _git(repo, "archive", "--format=tar", commit)

    entries: dict[str, tuple[bytes, bool]] = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        for member in tar:
            if member.isdir():
                continue
            if not member.isfile():
                # Symlinks and specials have no safe meaning inside a plugin
                # ZIP that Moodle will extract; refuse rather than guess.
                raise BuildError(f"unsupported entry in source tree: {member.name} ({member.type!r})")
            fileobj = tar.extractfile(member)
            assert fileobj is not None
            entries[member.name] = (fileobj.read(), bool(member.mode & 0o100))

    if "version.php" not in entries:
        raise BuildError("source tree has no version.php at its root; not a Moodle plugin?")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name in sorted(entries):
            content, executable = entries[name]
            info = zipfile.ZipInfo(f"{folder}/{name}", date_time=date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info._compresslevel = 9
            info.create_system = 3  # unix
            info.external_attr = (0o755 if executable else 0o644) << 16
            zf.writestr(info, content)

    data = buffer.getvalue()
    return BuiltArtifact(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        commit=commit,
        file_count=len(entries),
    )


def file_sha256_at_commit(repo: str, commit: str, path: str) -> str | None:
    """SHA-256 of a file's blob content at a commit, or None if absent."""
    result = subprocess.run(
        ["git", "-C", repo, "show", f"{commit}:{path}"],
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return hashlib.sha256(result.stdout).hexdigest()
