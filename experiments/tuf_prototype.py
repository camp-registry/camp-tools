"""TUF signing prototype (RFC §4.3) — Phase 2 spike, NOT production code.

Demonstrates the full metadata lifecycle over real camp artifacts with
throwaway in-memory ed25519 keys:

  1. generate one dev key per role (root / targets / snapshot / timestamp),
  2. describe the built artifacts + Composer metadata as TUF targets
     (length + sha256, the same hashes the index ledger records),
  3. sign and serialize the four metadata files to an output directory,
  4. verify the whole chain back from root, then demonstrate that a
     tampered targets file fails verification.

Production differences, so nobody mistakes the spike for the design:
  - root threshold here is 1; the RFC requires threshold signatures
    (2-of-N) with keys held offline by geographically separate stewards,
  - keys here are ephemeral; real keys live in HSMs / offline media with
    documented ceremonies and an audited signing path,
  - expiry here is short and uniform; production wants role-appropriate
    windows (long root, short timestamp) and automated re-signing.

Run:  python experiments/tuf_prototype.py <targets-dir> <out-dir>
"""

import datetime
import hashlib
import json
import sys
from pathlib import Path

from securesystemslib.signer import CryptoSigner
from tuf.api.metadata import (
    Metadata, MetaFile, Root, Snapshot, TargetFile, Targets, Timestamp,
)
from tuf.api.serialization.json import JSONSerializer

EXPIRY = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=7)
ROLES = ["root", "targets", "snapshot", "timestamp"]


def main(targets_dir: str, out_dir: str) -> int:
    targets_path = Path(targets_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    print("1. generating throwaway ed25519 keys (one per role)")
    signers = {role: CryptoSigner.generate_ed25519() for role in ROLES}

    print("2. describing artifacts as TUF targets")
    targets = Targets(expires=EXPIRY)
    for path in sorted(targets_path.rglob("*")):
        if path.is_file():
            name = path.relative_to(targets_path).as_posix()
            targets.targets[name] = TargetFile.from_file(name, str(path))
            print(f"   + {name} ({path.stat().st_size} bytes)")
    if not targets.targets:
        print("no target files found — build artifacts first (camp build / camp composer)")
        return 1

    snapshot = Snapshot(expires=EXPIRY)
    timestamp = Timestamp(expires=EXPIRY)
    root = Root(expires=EXPIRY)
    for role, signer in signers.items():
        root.add_key(signer.public_key, role)

    print("3. signing and serializing metadata")
    metadata = {
        "targets": Metadata(targets),
        "snapshot": Metadata(snapshot),
        "timestamp": Metadata(timestamp),
        "root": Metadata(root),
    }
    serializer = JSONSerializer(compact=False)
    # sign inner roles first so snapshot/timestamp can reference final bytes
    metadata["targets"].sign(signers["targets"])
    targets_bytes = metadata["targets"].to_bytes(serializer)
    snapshot.meta["targets.json"] = MetaFile(
        version=1, length=len(targets_bytes),
        hashes={"sha256": hashlib.sha256(targets_bytes).hexdigest()})
    metadata["snapshot"].sign(signers["snapshot"])
    snapshot_bytes = metadata["snapshot"].to_bytes(serializer)
    timestamp.snapshot_meta = MetaFile(
        version=1, length=len(snapshot_bytes),
        hashes={"sha256": hashlib.sha256(snapshot_bytes).hexdigest()})
    metadata["timestamp"].sign(signers["timestamp"])
    metadata["root"].sign(signers["root"])

    for role, md in metadata.items():
        md.to_file(str(out / f"{role}.json"), serializer)
        print(f"   wrote {out / f'{role}.json'}")

    print("4. verifying the chain from root")
    loaded = {role: Metadata.from_file(str(out / f"{role}.json")) for role in ROLES}
    loaded_root = loaded["root"].signed
    loaded_root.verify_delegate("root", loaded["root"].signed_bytes,
                                loaded["root"].signatures)
    for role in ("targets", "snapshot", "timestamp"):
        loaded_root.verify_delegate(role, loaded[role].signed_bytes,
                                    loaded[role].signatures)
        print(f"   ✓ {role}.json verifies against root")

    print("5. tamper check: flipping one byte of targets.json must fail")
    tampered = json.loads((out / "targets.json").read_text())
    first_target = next(iter(tampered["signed"]["targets"]))
    tampered["signed"]["targets"][first_target]["hashes"]["sha256"] = "0" * 64
    try:
        loaded_root.verify_delegate(
            "targets", json.dumps(tampered["signed"], separators=(",", ":"),
                                  sort_keys=True).encode(),
            {sig["keyid"]: loaded["targets"].signatures[sig["keyid"]]
             for sig in tampered["signatures"]})
        print("   ✗ tampered metadata verified — THIS IS A BUG")
        return 1
    except Exception as exc:
        print(f"   ✓ tampered targets rejected ({type(exc).__name__})")

    print("\ndone: full sign→verify→tamper-detect cycle demonstrated")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
