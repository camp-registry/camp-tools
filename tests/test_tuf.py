"""TUF repository signing: roundtrip, threshold, tamper detection."""

import pytest

from camp.tuf_repo import init_keys, sign_repository, verify_repository


@pytest.fixture
def signed_repo(tmp_path):
    targets = tmp_path / "targets"
    targets.mkdir()
    (targets / "a.zip").write_bytes(b"artifact-a")
    (targets / "sub").mkdir()
    (targets / "sub" / "b.json").write_text("{}")
    keys = tmp_path / "keys"
    meta = tmp_path / "meta"
    init_keys(keys, root_keys=3, threshold=2)
    sign_repository(targets, keys, meta)
    return targets, keys, meta


def test_sign_verify_roundtrip(signed_repo):
    targets, _, meta = signed_repo
    assert verify_repository(meta, targets) == []


def test_resign_bumps_versions(signed_repo):
    targets, keys, meta = signed_repo
    versions = sign_repository(targets, keys, meta)
    assert versions["targets"] == 2 and versions["root"] == 2
    assert verify_repository(meta, targets) == []


def test_tampered_target_detected(signed_repo):
    targets, _, meta = signed_repo
    (targets / "a.zip").write_bytes(b"artifact-EVIL")
    problems = verify_repository(meta, targets)
    assert any("does not match signed hash" in p for p in problems)


def test_missing_target_detected(signed_repo):
    targets, _, meta = signed_repo
    (targets / "a.zip").unlink()
    assert any("missing" in p for p in verify_repository(meta, targets))


def test_foreign_metadata_rejected(signed_repo, tmp_path):
    """Metadata signed by different keys must not verify."""
    targets, _, meta = signed_repo
    other_keys = tmp_path / "other-keys"
    other_meta = tmp_path / "other-meta"
    init_keys(other_keys)
    sign_repository(targets, other_keys, other_meta)
    # splice: foreign targets.json into our metadata dir
    (meta / "targets.json").write_bytes((other_meta / "targets.json").read_bytes())
    assert any("targets.json signature invalid" in p
               for p in verify_repository(meta, targets))


def test_threshold_validation(tmp_path):
    with pytest.raises(ValueError):
        init_keys(tmp_path / "k", root_keys=2, threshold=3)
