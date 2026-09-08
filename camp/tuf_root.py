"""Root ceremony tooling: the host side of `camp tuf root` (RFC §4.3, §9).

Stewards never run camp. Each generates an ECDSA P-256 key inside a
hardware token (PIV slot 9c), hands over the public key as PEM, and later
signs an exact byte string with `pkcs11-tool --signature-format openssl`,
which yields a DER-encoded ECDSA signature. This module does everything
around that:

  camp tuf root build OUT --steward NAME=PUB.pem ... --online-keys DIR
                          --threshold 3 [--expires-days 365] [--previous ROOT]
      Assemble the unsigned root: steward keys under the root role, the
      online role keys (targets/snapshot/timestamp) from DIR, the
      threshold and expiry. Writes OUT/root.json (unsigned), the canonical
      OUT/root-payload.bin every steward hashes and signs, and
      OUT/stewards.json (name -> keyid). Prints the payload SHA-256 for the
      read-back. With --previous the version increments and the result is
      a rotation candidate.

  camp tuf root check ROOT.json [--stewards STEWARDS.json] [--previous ROOT]
      What a reader needs to confirm before signing: payload SHA-256,
      version, expiry, threshold, every key by role, and the difference
      from the previous root when one is given.

  camp tuf root assemble OUT --sig NAME=FILE.sig ... [--previous ROOT]
      Attach steward signatures (DER files) to OUT/root.json, verify each
      one against its key, then verify the threshold of the new root and,
      for a rotation, of the previous root too (TUF root update rule).
      Writes the signed OUT/root.json and OUT/<version>.root.json only
      when both thresholds are met.

Steward signatures are plain DER, so the ceremony never depends on camp
being installed on a steward's machine; the host verifies with the same
library the clients use.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from securesystemslib.signer import Signature, SSlibKey
from tuf.api.metadata import Metadata, Root
from tuf.api.serialization.json import JSONSerializer

from .tuf_repo import ONLINE_ROLES

ROOT_EXPIRY_DAYS = 365
_SERIALIZER = JSONSerializer(compact=False)


class CeremonyError(Exception):
    pass


def load_public_key(path: str | Path) -> SSlibKey:
    """A securesystemslib key from a PEM file holding either a public key
    (what yubico-piv-tool -a generate writes) or a private key (dev keys)."""
    data = Path(path).read_bytes()
    try:
        public = serialization.load_pem_public_key(data)
    except ValueError:
        try:
            public = serialization.load_pem_private_key(data, password=None).public_key()
        except ValueError as exc:
            raise CeremonyError(f"{path}: not a PEM public or private key") from exc
    return SSlibKey.from_crypto(public)


def _online_public_keys(keys_dir: str | Path) -> dict[str, SSlibKey]:
    keys = Path(keys_dir)
    found = {}
    for role in ONLINE_ROLES:
        for name in (f"{role}.pub", f"{role}.pub.pem", f"{role}.pem"):
            if (keys / name).exists():
                found[role] = load_public_key(keys / name)
                break
        else:
            raise CeremonyError(f"no key for online role '{role}' in {keys} "
                                f"(expected {role}.pub, {role}.pub.pem or {role}.pem)")
    return found


def _expiry(days: int) -> datetime.datetime:
    return (datetime.datetime.now(datetime.UTC).replace(microsecond=0)
            + datetime.timedelta(days=days))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_root(path: str | Path) -> Metadata:
    md = Metadata.from_file(str(path))
    if not isinstance(md.signed, Root):
        raise CeremonyError(f"{path} is not root metadata")
    return md


def build_root(out_dir: str | Path, stewards: dict[str, str | Path],
               online_keys_dir: str | Path, threshold: int,
               expires_days: int = ROOT_EXPIRY_DAYS,
               previous: str | Path | None = None) -> dict:
    """Write the unsigned root, its canonical payload and the steward map.
    Returns a summary (also what `check` prints)."""
    if threshold < 1 or threshold > len(stewards):
        raise CeremonyError(f"threshold {threshold} must be between 1 and the "
                            f"number of stewards ({len(stewards)})")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    version = 1
    if previous is not None:
        version = _load_root(previous).signed.version + 1

    root = Root(version=version, expires=_expiry(expires_days))
    steward_ids: dict[str, str] = {}
    for name, pem in stewards.items():
        key = load_public_key(pem)
        if key.scheme != "ecdsa-sha2-nistp256":
            raise CeremonyError(f"{name}: expected an ECDSA P-256 key, got {key.scheme}")
        if key.keyid in steward_ids.values():
            raise CeremonyError(f"{name}: duplicate key (same as another steward)")
        root.add_key(key, "root")
        steward_ids[name] = key.keyid
    for role, key in _online_public_keys(online_keys_dir).items():
        root.add_key(key, role)
    root.roles["root"].threshold = threshold

    md = Metadata(root)
    payload = md.signed_bytes
    md.to_file(str(out / "root.json"), _SERIALIZER)
    (out / "root-payload.bin").write_bytes(payload)
    (out / "stewards.json").write_text(json.dumps(steward_ids, indent=2) + "\n")
    return describe(out / "root.json", out / "stewards.json", previous)


def describe(root_json: str | Path, stewards_json: str | Path | None = None,
             previous: str | Path | None = None) -> dict:
    """Facts a signer confirms before signing, plus a diff against the
    previous root when given."""
    md = _load_root(root_json)
    root = md.signed
    payload = md.signed_bytes
    names: dict[str, str] = {}
    if stewards_json and Path(stewards_json).exists():
        names = {kid: name for name, kid in json.loads(Path(stewards_json).read_text()).items()}
    roles = {}
    for role_name, role in root.roles.items():
        roles[role_name] = {
            "threshold": role.threshold,
            "keys": [{"keyid": kid, "name": names.get(kid), "scheme": root.keys[kid].scheme}
                     for kid in role.keyids],
        }
    summary = {
        "payload-sha256": _sha256(payload),
        "version": root.version,
        "expires": root.expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "root-threshold": root.roles["root"].threshold,
        "roles": roles,
        "signatures": sorted(md.signatures),
    }
    if previous is not None:
        prev = _load_root(previous).signed
        old_root = set(prev.roles["root"].keyids)
        new_root = set(root.roles["root"].keyids)
        online_changed = [r for r in ONLINE_ROLES
                          if set(prev.roles[r].keyids) != set(root.roles[r].keyids)]
        summary["changes-from-previous"] = {
            "previous-version": prev.version,
            "root-keys-added": sorted(new_root - old_root),
            "root-keys-removed": sorted(old_root - new_root),
            "threshold": [prev.roles["root"].threshold, root.roles["root"].threshold],
            "expires": [prev.expires.strftime("%Y-%m-%dT%H:%M:%SZ"), summary["expires"]],
            "online-roles-changed": online_changed,
        }
    return summary


def format_summary(summary: dict) -> str:
    lines = [
        f"payload sha256   {summary['payload-sha256']}",
        f"version          {summary['version']}",
        f"expires          {summary['expires']}",
        f"root threshold   {summary['root-threshold']} of "
        f"{len(summary['roles']['root']['keys'])}",
    ]
    for role_name, role in summary["roles"].items():
        for key in role["keys"]:
            who = f"  ({key['name']})" if key.get("name") else ""
            lines.append(f"  {role_name:10} {key['keyid'][:16]}…  {key['scheme']}{who}")
    if summary.get("signatures"):
        lines.append(f"signatures       {len(summary['signatures'])} attached")
    changes = summary.get("changes-from-previous")
    if changes:
        lines.append(f"since v{changes['previous-version']}:")
        lines.append(f"  root keys added    {[k[:16] for k in changes['root-keys-added']] or 'none'}")
        lines.append(f"  root keys removed  {[k[:16] for k in changes['root-keys-removed']] or 'none'}")
        lines.append(f"  threshold          {changes['threshold'][0]} -> {changes['threshold'][1]}")
        lines.append(f"  expires            {changes['expires'][0]} -> {changes['expires'][1]}")
        lines.append(f"  online roles changed  {changes['online-roles-changed'] or 'none'}")
    return "\n".join(lines)


def assemble_root(out_dir: str | Path, signatures: dict[str, str | Path],
                  previous: str | Path | None = None) -> dict:
    """Attach steward DER signatures, verify each, verify the threshold(s),
    and write the signed root only when they hold. Returns a report; the
    'complete' flag says whether the root is usable."""
    out = Path(out_dir)
    md = _load_root(out / "root.json")
    root = md.signed
    payload = md.signed_bytes
    payload_file = out / "root-payload.bin"
    if payload_file.exists() and payload_file.read_bytes() != payload:
        raise CeremonyError("root-payload.bin does not match root.json; the root was "
                            "edited after the payload was distributed")
    steward_ids = json.loads((out / "stewards.json").read_text())

    accepted, rejected = [], []
    for name, sig_path in signatures.items():
        keyid = steward_ids.get(name)
        if keyid is None:
            rejected.append((name, "not a steward in stewards.json"))
            continue
        der = Path(sig_path).read_bytes()
        sig = Signature(keyid, der.hex())
        try:
            root.keys[keyid].verify_signature(sig, payload)
        except Exception as exc:  # securesystemslib raises its own hierarchy
            rejected.append((name, f"signature does not verify: {type(exc).__name__}"))
            continue
        md.signatures[keyid] = sig
        accepted.append(name)

    new_result = root.get_verification_result("root", payload, md.signatures)
    report = {
        "accepted": accepted,
        "rejected": rejected,
        "new-root": {"verified": new_result.verified,
                     "signed": len(new_result.signed),
                     "threshold": root.roles["root"].threshold},
        "complete": new_result.verified,
    }
    if previous is not None:
        prev = _load_root(previous).signed
        old_result = prev.get_verification_result("root", payload, md.signatures)
        report["previous-root"] = {"verified": old_result.verified,
                                   "signed": len(old_result.signed),
                                   "threshold": prev.roles["root"].threshold}
        report["complete"] = new_result.verified and old_result.verified

    if report["complete"]:
        md.to_file(str(out / "root.json"), _SERIALIZER)
        md.to_file(str(out / f"{root.version}.root.json"), _SERIALIZER)
        report["written"] = [str(out / "root.json"), str(out / f"{root.version}.root.json")]
    return report


def format_report(report: dict) -> str:
    lines = [f"accepted signatures: {', '.join(report['accepted']) or 'none'}"]
    for name, why in report["rejected"]:
        lines.append(f"REJECTED {name}: {why}")
    n = report["new-root"]
    lines.append(f"new root threshold:      {n['signed']} of {n['threshold']} "
                 f"{'met' if n['verified'] else 'NOT met'}")
    if "previous-root" in report:
        p = report["previous-root"]
        lines.append(f"previous root threshold: {p['signed']} of {p['threshold']} "
                     f"{'met' if p['verified'] else 'NOT met'}")
    lines.append("root COMPLETE and written" if report["complete"]
                 else "root NOT written: threshold not met")
    return "\n".join(lines)
