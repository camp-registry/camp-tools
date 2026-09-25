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
# Phase-2 windows (camp-tools#47, decided 2026-09-25): the timestamp is the
# freeze-attack bound and the availability budget in one number. Publish runs
# twice daily, so 14 days is ~28 missed runs of slack; the dead-man's switch
# fires ~30 h after a missed run, leaving ~12 days to repair. Tighten later by
# passing `expiry` from the publish workflow; the root's expiry is set at the
# ceremony and never written here.
EXPIRY_DAYS = {"root": 365, "targets": 90, "snapshot": 30, "timestamp": 14}
_SERIALIZER = JSONSerializer(compact=False)


def parse_expiry(specs) -> dict[str, int]:
    """`role=days` strings (CLI / workflow env) → override dict, validated."""
    out: dict[str, int] = {}
    for spec in specs or []:
        role, _, days = str(spec).partition("=")
        role = role.strip()
        if role not in ONLINE_ROLES:
            raise ValueError(f"expiry role must be one of {ONLINE_ROLES}: {spec!r}")
        try:
            value = int(days)
        except ValueError:
            raise ValueError(f"expiry days must be an integer: {spec!r}") from None
        if value < 1:
            raise ValueError(f"expiry days must be >= 1: {spec!r}")
        out[role] = value
    if out:
        ts = out.get("timestamp", EXPIRY_DAYS["timestamp"])
        sn = out.get("snapshot", EXPIRY_DAYS["snapshot"])
        tg = out.get("targets", EXPIRY_DAYS["targets"])
        if not ts <= sn <= tg:
            raise ValueError(
                f"expiry must satisfy timestamp <= snapshot <= targets, got "
                f"timestamp={ts} snapshot={sn} targets={tg}")
    return out


def _expires(role: str, expiry: dict[str, int] | None = None) -> datetime.datetime:
    days = (expiry or {}).get(role, EXPIRY_DAYS[role])
    return datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=days)


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
    """Dev/staging layout: root private keys live beside the online ones.
    In production there are no root-*.pem files — the root is the
    steward-signed metadata already in the metadata directory (see
    sign_repository) — so an empty root list is not an error here."""
    root_signers = [_load_signer(path) for path in sorted(keys_dir.glob("root-*.pem"))]
    online = {role: _load_signer(keys_dir / f"{role}.pem") for role in ONLINE_ROLES}
    threshold_file = keys_dir / "THRESHOLD"
    threshold = int(threshold_file.read_text()) if threshold_file.exists() else 1
    return root_signers, online, threshold


def _next_version(metadata_dir: Path, role: str) -> int:
    path = metadata_dir / f"{role}.json"
    if not path.exists():
        return 1
    return Metadata.from_file(str(path)).signed.version + 1


def sign_repository(targets_dir: str | Path, keys_dir: str | Path,
                    metadata_dir: str | Path, *,
                    only: list[str] | None = None,
                    extra: dict[str, TargetFile] | None = None,
                    expiry: dict[str, int] | None = None) -> dict[str, int]:
    """Sign the online roles over a target set and write the metadata.

    Targets are every file under `targets_dir` (the dev/test shape), or,
    when `only` is given, just those relative paths (production: the few
    machine-readable files of the site tree, not 6,000 HTML pages), plus
    `extra` target files whose bytes live elsewhere (the ledger's ZIPs on
    the artifact host; see tuf_targets.ledger_targets).

    The root's consistent_snapshot is true, so targets and snapshot are also
    written as `<version>.<role>.json`, root as `<version>.root.json`, and
    timestamp only unversioned — the layout python-tuf clients expect.
    `expiry` overrides EXPIRY_DAYS per online role (never root)."""
    targets_path = Path(targets_dir)
    out = Path(metadata_dir)
    out.mkdir(parents=True, exist_ok=True)
    root_signers, online, threshold = _load_all(Path(keys_dir))

    targets = Targets(expires=_expires("targets", expiry),
                      version=_next_version(out, "targets"))
    if only is None:
        paths = [p for p in sorted(targets_path.rglob("*")) if p.is_file()]
    else:
        paths = []
        for rel in only:
            path = targets_path / rel
            if not path.is_file():
                raise FileNotFoundError(f"target listed in --only is missing: {rel}")
            paths.append(path)
    for path in paths:
        name = path.relative_to(targets_path).as_posix()
        targets.targets[name] = TargetFile.from_file(name, str(path))
    for name, target in (extra or {}).items():
        targets.targets[name] = target

    ceremony_root: Metadata | None = None
    if root_signers:
        root = Root(expires=_expires("root"), version=_next_version(out, "root"))
        for signer in root_signers:
            root.add_key(signer.public_key, "root")
        for role, signer in online.items():
            root.add_key(signer.public_key, role)
        root.roles["root"].threshold = threshold
    else:
        # Production: the root was signed by the stewards (camp tuf root
        # assemble) and is never regenerated here; this run only signs the
        # online roles, whose keys must be the ones that root lists.
        root_path = out / "root.json"
        if not root_path.exists():
            raise FileNotFoundError(
                f"no root-*.pem keys in {keys_dir} and no steward-signed "
                f"{root_path}; run `camp tuf init` (dev) or the root ceremony")
        ceremony_root = Metadata.from_file(str(root_path))
        root = ceremony_root.signed
        for role, signer in online.items():
            if signer.public_key.keyid not in root.roles[role].keyids:
                raise ValueError(f"{role}.pem in {keys_dir} is not the {role} key "
                                 f"listed in {root_path}")

    md_targets = Metadata(targets)
    for signer in [online["targets"]]:
        md_targets.sign(signer)
    targets_bytes = md_targets.to_bytes(_SERIALIZER)

    snapshot = Snapshot(expires=_expires("snapshot", expiry),
                        version=_next_version(out, "snapshot"))
    snapshot.meta["targets.json"] = MetaFile(
        version=targets.version, length=len(targets_bytes),
        hashes={"sha256": hashlib.sha256(targets_bytes).hexdigest()})
    md_snapshot = Metadata(snapshot)
    md_snapshot.sign(online["snapshot"])
    snapshot_bytes = md_snapshot.to_bytes(_SERIALIZER)

    timestamp = Timestamp(expires=_expires("timestamp", expiry),
                          version=_next_version(out, "timestamp"))
    timestamp.snapshot_meta = MetaFile(
        version=snapshot.version, length=len(snapshot_bytes),
        hashes={"sha256": hashlib.sha256(snapshot_bytes).hexdigest()})
    md_timestamp = Metadata(timestamp)
    md_timestamp.sign(online["timestamp"])

    if ceremony_root is not None:
        md_root = ceremony_root          # steward signatures, untouched
    else:
        md_root = Metadata(root)
        for signer in root_signers:
            md_root.sign(signer, append=True)

    for role, md in [("root", md_root), ("targets", md_targets),
                     ("snapshot", md_snapshot), ("timestamp", md_timestamp)]:
        md.to_file(str(out / f"{role}.json"), _SERIALIZER)
        if role != "timestamp":
            # consistent_snapshot layout; the unversioned copy above is
            # the convenience "latest" for humans and for _next_version.
            md.to_file(str(out / f"{md.signed.version}.{role}.json"), _SERIALIZER)
    return {"root": root.version, "targets": targets.version,
            "snapshot": snapshot.version, "timestamp": timestamp.version,
            "target-files": len(targets.targets)}


def verify_repository(metadata_dir: str | Path, targets_dir: str | Path, *,
                      missing_ok_suffixes: tuple[str, ...] = ()) -> list[str]:
    """Full client-style verification. Returns a list of problems.

    Targets whose name ends with one of `missing_ok_suffixes` and are not
    under `targets_dir` are skipped, not reported: in publish the ZIPs live
    on the artifact host and were just audited hash-exact there, while the
    site-tree targets are on disk and must match."""
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
            if missing_ok_suffixes and name.endswith(missing_ok_suffixes):
                continue
            problems.append(f"target missing on disk: {name}")
            continue
        try:
            target.verify_length_and_hashes(local.read_bytes())
        except Exception:
            problems.append(f"target does not match signed hash: {name}")
    return problems
