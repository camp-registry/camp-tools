"""The artifact archive (D22): append-only object storage for every ZIP.

The archive is the registry's permanent-storage commitment (RFC §2.2):
every ledger release — revoked ones included, preserved for forensics —
is deposited exactly once and never modified. Three layers enforce
append-only: this code never overwrites an existing key; the CI
application key carries no delete capability; and the bucket's
compliance-mode object lock makes deposited bytes platform-immutable
for the retention period regardless of credentials.

Deposits ride the merge-triggered publish (a release merge *is* the
deposit moment); the daily publish re-runs deposit as a no-op and then
audits — a HEAD per release comparing the stored sha256 against the
ledger — so a hole in the archive turns the publish red the same day.

Talks S3 protocol (B2's S3-compatible endpoint) through an injected
client; boto3 is an optional dependency (`camp-tools[archive]`), and
tests use an in-memory fake.
"""

from __future__ import annotations

import datetime
import hashlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .artifacts import artifact_relpath
from .build import BuildError, build_zip
from .validate import load_entry
from .verify import _clone


@dataclass
class ArchiveResult:
    deposited: int = 0
    present: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


# Every deposited object gets an explicit compliance-mode lock for this
# long, independent of any bucket default. Extend by re-depositing epochs
# before expiry; never shorten (the platform refuses anyway).
RETENTION_YEARS = 7


def s3_store(bucket: str, endpoint: str, key_id: str, application_key: str):
    """A minimal store over any S3-compatible endpoint (B2 here)."""
    import boto3

    client = boto3.client(
        "s3", endpoint_url=f"https://{endpoint}",
        aws_access_key_id=key_id, aws_secret_access_key=application_key)

    class _Store:
        def head(self, key: str) -> dict | None:
            """Object metadata, or None if the key doesn't exist."""
            try:
                response = client.head_object(Bucket=bucket, Key=key)
            except client.exceptions.ClientError as exc:
                code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if code == 404:
                    return None
                raise
            return {k.lower(): v for k, v in
                    (response.get("Metadata") or {}).items()}

        def put(self, key: str, data: bytes, sha256: str) -> None:
            retain_until = (datetime.datetime.now(datetime.UTC)
                            + datetime.timedelta(days=365 * RETENTION_YEARS))
            client.put_object(
                Bucket=bucket, Key=key, Body=data,
                ContentType="application/zip",
                CacheControl="public, max-age=31536000, immutable",
                ObjectLockMode="COMPLIANCE",
                ObjectLockRetainUntilDate=retain_until,
                Metadata={"sha256": sha256})

    return _Store()


def _releases(index_dir: str | Path):
    for entry_path in sorted(Path(index_dir).glob("plugins/*/*.yml")):
        entry = load_entry(entry_path)
        # Every ledger release belongs in the archive — revoked and
        # delisted included. Distribution exclusion is the site's and
        # composer's job; the archive keeps everything, forever.
        for release in entry.get("releases") or []:
            yield entry, release


def deposit(index_dir: str | Path, store, log=print) -> ArchiveResult:
    """Deposit every ledger release not already archived.

    Existing keys are never rewritten: a present object with a matching
    sha256 is counted and skipped; a mismatch is a hard problem (the
    object lock should make it impossible — seeing one means something
    is deeply wrong and a human must look)."""
    result = ArchiveResult()
    by_source: dict[str, list] = {}
    for entry, release in _releases(index_dir):
        key = artifact_relpath(entry["component"], release)
        existing = store.head(key)
        if existing is not None:
            if existing.get("sha256") != release["zip-sha256"]:
                result.problems.append(
                    f"{key}: archived sha256 {existing.get('sha256')} != "
                    f"ledger {release['zip-sha256']} — REFUSING to touch; "
                    "investigate before anything else")
            else:
                result.present += 1
            continue
        by_source.setdefault(entry["source"], []).append((entry, release, key))

    for source, items in by_source.items():
        with tempfile.TemporaryDirectory(prefix="camp-deposit-") as tmp:
            repo = str(Path(tmp) / "src")
            try:
                _clone(source, repo)
            except BuildError as exc:
                result.problems.append(f"{source}: {exc}")
                continue
            for entry, release, key in items:
                try:
                    artifact = build_zip(repo, release["tag"], entry["component"])
                except BuildError as exc:
                    result.problems.append(f"{key}: build failed: {exc}")
                    continue
                if artifact.sha256 != release["zip-sha256"]:
                    result.problems.append(
                        f"{key}: rebuilt sha256 {artifact.sha256} != ledger "
                        f"{release['zip-sha256']} — not deposited")
                    continue
                store.put(key, artifact.data, artifact.sha256)
                result.deposited += 1
                log(f"deposit: {key} ({len(artifact.data)} bytes)")
    return result


def audit(index_dir: str | Path, store, log=print) -> ArchiveResult:
    """Verify the archive holds every ledger release with the right hash."""
    result = ArchiveResult()
    for entry, release in _releases(index_dir):
        key = artifact_relpath(entry["component"], release)
        existing = store.head(key)
        if existing is None:
            result.problems.append(f"{key}: MISSING from the archive")
        elif existing.get("sha256") != release["zip-sha256"]:
            result.problems.append(
                f"{key}: archived sha256 {existing.get('sha256')} != "
                f"ledger {release['zip-sha256']}")
        else:
            result.present += 1
    return result
