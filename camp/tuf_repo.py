"""TUF repository signing (RFC §4.3) — production-shaped, dev-key bootstrap.

Promotes the Phase 2 spike into real tooling:

  camp tuf init <keys-dir> [--root-keys N] [--threshold M]
      Generate per-role ed25519 keys. Root gets N keys with an M-of-N
      threshold, mirroring the RFC's steward model.

  camp tuf sign <targets-dir> <keys-dir> <metadata-dir>
      Describe every file under targets-dir as a TUF target and write signed
      root/targets/snapshot/timestamp metadata. Versions bump automatically
      on re-signing.

  camp tuf verify <metadata-dir> <targets-dir>
      Verify the signature chain from root, then verify every on-disk
      target against the signed hashes — what a client/mirror-auditor does.

PRODUCTION CAVEAT, in code because it matters: keys written by `init` are
PLAINTEXT PEM on one machine. That bootstraps development and CI staging
only. Launch requires the documented ceremony: root keys generated offline
by separate stewards, threshold M >= 2, and only online roles' keys
(snapshot/timestamp) on infrastructure.
"""

from __future__ import annotations

import datetime
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from securesystemslib.signer import CryptoSigner
from tuf.api.metadata import (
    Metadata, MetaFile, Root, Snapshot, TargetFile, Targets, Timestamp,
)
from tuf.api.serialization.json import JSONSerializer

ONLINE_ROLES = ["targets", "snapshot", "timestamp"]
EXPIRY_DAYS = {"root": 365, "targets": 90, "snapshot": 7, "timestamp": 1}
_SERIALIZER = JSONSerializer(compact=False)


def _expires(role: str) -> datetime.datetime:
    return (datetime.datetime.now(datetime.UTC)
            + datetime.timedelta(days=EXPIRY_DAYS[role]))


def _write_key(path: Path, signer: CryptoSigner) -> None:
    pem = signer.private_bytes
    path.write_bytes(pem)
    os.chmod(path, 0o600)


def _load_signer(path: Path) -> CryptoSigner:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    return CryptoSigner(key)


def init_keys(keys_dir: str | Path, root_keys: int = 1, threshold: int = 1) -> list[str]:
    if threshold > root_keys:
        raise ValueError(f"threshold {threshold} exceeds root key count {root_keys}")
    keys = Path(keys_dir)
    keys.mkdir(parents=True, exist_ok=True)
    written = []
    for index in range(1, root_keys + 1):
        path = keys / f"root-{index}.pem"
        _write_key(path, CryptoSigner.generate_ed25519())
        written.append(str(path))
    for role in ONLINE_ROLES:
        path = keys / f"{role}.pem"
        _write_key(path, CryptoSigner.generate_ed25519())
        written.append(str(path))
    (keys / "THRESHOLD").write_text(f"{threshold}\n")
    (keys / "WARNING.txt").write_text(
        "Plaintext dev/staging keys. Production root keys are generated\n"
        "offline by separate stewards in a documented ceremony (RFC §4.3, §9).\n")
    return written


def _load_all(keys_dir: Path) -> tuple[list[CryptoSigner], dict[str, CryptoSigner], int]:
    root_signers = [_load_signer(path) for path in sorted(keys_dir.glob("root-*.pem"))]
    online = {role: _load_signer(keys_dir / f"{role}.pem") for role in ONLINE_ROLES}
    threshold_file = keys_dir / "THRESHOLD"
    threshold = int(threshold_file.read_text()) if threshold_file.exists() else 1
    if not root_signers:
        raise FileNotFoundError(f"no root-*.pem keys in {keys_dir} — run `camp tuf init`")
    return root_signers, online, threshold


def _next_version(metadata_dir: Path, role: str) -> int:
    path = metadata_dir / f"{role}.json"
    if not path.exists():
        return 1
    return Metadata.from_file(str(path)).signed.version + 1


def sign_repository(targets_dir: str | Path, keys_dir: str | Path,
                    metadata_dir: str | Path) -> dict[str, int]:
    targets_path = Path(targets_dir)
    out = Path(metadata_dir)
    out.mkdir(parents=True, exist_ok=True)
    root_signers, online, threshold = _load_all(Path(keys_dir))

    targets = Targets(expires=_expires("targets"),
                      version=_next_version(out, "targets"))
    for path in sorted(targets_path.rglob("*")):
        if path.is_file():
            name = path.relative_to(targets_path).as_posix()
            targets.targets[name] = TargetFile.from_file(name, str(path))

    root = Root(expires=_expires("root"), version=_next_version(out, "root"))
    for signer in root_signers:
        root.add_key(signer.public_key, "root")
    for role, signer in online.items():
        root.add_key(signer.public_key, role)
    root.roles["root"].threshold = threshold

    md_targets = Metadata(targets)
    for signer in [online["targets"]]:
        md_targets.sign(signer)
    targets_bytes = md_targets.to_bytes(_SERIALIZER)

    snapshot = Snapshot(expires=_expires("snapshot"),
                        version=_next_version(out, "snapshot"))
    snapshot.meta["targets.json"] = MetaFile(
        version=targets.version, length=len(targets_bytes),
        hashes={"sha256": hashlib.sha256(targets_bytes).hexdigest()})
    md_snapshot = Metadata(snapshot)
    md_snapshot.sign(online["snapshot"])
    snapshot_bytes = md_snapshot.to_bytes(_SERIALIZER)

    timestamp = Timestamp(expires=_expires("timestamp"),
                          version=_next_version(out, "timestamp"))
    timestamp.snapshot_meta = MetaFile(
        version=snapshot.version, length=len(snapshot_bytes),
        hashes={"sha256": hashlib.sha256(snapshot_bytes).hexdigest()})
    md_timestamp = Metadata(timestamp)
    md_timestamp.sign(online["timestamp"])

    md_root = Metadata(root)
    for signer in root_signers:
        md_root.sign(signer, append=True)

    for role, md in [("root", md_root), ("targets", md_targets),
                     ("snapshot", md_snapshot), ("timestamp", md_timestamp)]:
        md.to_file(str(out / f"{role}.json"), _SERIALIZER)
    return {"root": root.version, "targets": targets.version,
            "snapshot": snapshot.version, "timestamp": timestamp.version,
            "target-files": len(targets.targets)}


def verify_repository(metadata_dir: str | Path, targets_dir: str | Path) -> list[str]:
    """Full client-style verification. Returns a list of problems."""
    meta = Path(metadata_dir)
    problems: list[str] = []
    loaded = {role: Metadata.from_file(str(meta / f"{role}.json"))
              for role in ("root", "targets", "snapshot", "timestamp")}
    root = loaded["root"].signed

    for role in ("root", "targets", "snapshot", "timestamp"):
        try:
            root.verify_delegate(role, loaded[role].signed_bytes,
                                 loaded[role].signatures)
        except Exception as exc:
            problems.append(f"{role}.json signature invalid: {exc}")
    if problems:
        return problems

    now = datetime.datetime.now(datetime.UTC)
    for role, md in loaded.items():
        if md.signed.is_expired(now):
            problems.append(f"{role}.json is expired")

    targets_path = Path(targets_dir)
    for name, target in loaded["targets"].signed.targets.items():
        local = targets_path / name
        if not local.exists():
            problems.append(f"target missing on disk: {name}")
            continue
        try:
            target.verify_length_and_hashes(local.read_bytes())
        except Exception:
            problems.append(f"target does not match signed hash: {name}")
    return problems
