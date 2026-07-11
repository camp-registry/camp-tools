"""Rekor entry construction and local verification."""

from camp.rekor import build_entry, generate_key, verify_entry_locally


def test_entry_shape_and_self_verification(tmp_path):
    artifact = tmp_path / "a.zip"
    artifact.write_bytes(b"artifact bytes")
    key = tmp_path / "key.pem"
    generate_key(key)

    entry = build_entry(artifact, key)
    assert entry["kind"] == "hashedrekord"
    assert entry["spec"]["data"]["hash"]["algorithm"] == "sha256"
    assert len(entry["spec"]["data"]["hash"]["value"]) == 64
    assert verify_entry_locally(entry, artifact)


def test_verification_fails_for_different_artifact(tmp_path):
    artifact = tmp_path / "a.zip"
    artifact.write_bytes(b"artifact bytes")
    key = tmp_path / "key.pem"
    generate_key(key)
    entry = build_entry(artifact, key)

    artifact.write_bytes(b"DIFFERENT bytes")
    assert not verify_entry_locally(entry, artifact)


def test_verification_fails_for_tampered_signature(tmp_path):
    artifact = tmp_path / "a.zip"
    artifact.write_bytes(b"artifact bytes")
    key = tmp_path / "key.pem"
    generate_key(key)
    entry = build_entry(artifact, key)
    entry["spec"]["signature"]["content"] = entry["spec"]["signature"]["content"][:-8] + "AAAAAAA="
    assert not verify_entry_locally(entry, artifact)
