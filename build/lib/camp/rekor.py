"""Rekor transparency-log integration (RFC §4.3) — dry-run by default.

Builds `hashedrekord` entries for release artifacts: the artifact's sha256,
an ECDSA-P256 signature over the artifact, and the signing public key.
Submitting one makes the signing event permanently, publicly auditable on
rekor.sigstore.dev — anyone can later prove *when* the artifact was signed
and detect after-the-fact substitution.

Two deliberate limits:

  - Submission never happens implicitly. `camp rekor` prints the exact
    payload; `--submit` is required to POST it, because entries in the
    public log are forever and should carry the project's real identity,
    not a developer's laptop key.
  - ECDSA P-256, not ed25519: Rekor verifies hashedrekord signatures
    against the artifact *digest*, which pure ed25519 cannot do.

Production integration (Phase 2+): submission runs in CI after TUF signing,
with the entry UUID recorded back into the index so clients can
cross-check; maintainer identity comes from OIDC (Fulcio) rather than a
long-lived key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

REKOR_URL = "https://rekor.sigstore.dev/api/v1/log/entries"


def generate_key(path: str | Path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    Path(path).write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    Path(path).chmod(0o600)


def build_entry(artifact_path: str | Path, key_path: str | Path) -> dict:
    data = Path(artifact_path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()

    key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    signature = key.sign(data, ec.ECDSA(hashes.SHA256()))
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo)

    return {
        "apiVersion": "0.0.1",
        "kind": "hashedrekord",
        "spec": {
            "data": {"hash": {"algorithm": "sha256", "value": digest}},
            "signature": {
                "content": base64.b64encode(signature).decode(),
                "publicKey": {"content": base64.b64encode(public_pem).decode()},
            },
        },
    }


def verify_entry_locally(entry: dict, artifact_path: str | Path) -> bool:
    """Check the entry is internally consistent with the artifact — the same
    check Rekor performs on submission."""
    data = Path(artifact_path).read_bytes()
    spec = entry["spec"]
    if hashlib.sha256(data).hexdigest() != spec["data"]["hash"]["value"]:
        return False
    public_key = serialization.load_pem_public_key(
        base64.b64decode(spec["signature"]["publicKey"]["content"]))
    try:
        public_key.verify(base64.b64decode(spec["signature"]["content"]),
                          data, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


def submit(entry: dict, url: str = REKOR_URL) -> dict:
    """POST an entry to the transparency log. PUBLIC AND PERMANENT."""
    request = urllib.request.Request(
        url, data=json.dumps(entry).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "camp-rekor/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())
