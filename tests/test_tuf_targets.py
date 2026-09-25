"""Phase-2 signing shape (camp-tools#47): selective site targets, ledger
targets whose bytes live on the artifact host, consistent-snapshot files,
configurable expiry, and a verify that tolerates the off-host ZIPs."""

import datetime
import json

import pytest
import yaml
from tuf.api.metadata import Metadata

from camp.cli import main
from camp.tuf_repo import (EXPIRY_DAYS, init_keys, parse_expiry, sign_repository,
                           verify_repository)
from camp.tuf_targets import SITE_TARGETS, ledger_targets, served_releases


def _index(tmp_path, tier=2, status="active", revoke=False):
    index = tmp_path / "index"
    (index / "plugins" / "mod").mkdir(parents=True)
    entry = {
        "component": "mod_example", "source": "https://example.invalid/x",
        "tier": tier, "status": status, "maintainers": [{"github": "x"}],
        "labels": ["fully-free"],
        "releases": [
            {"version": "1.0.0", "tag": "v1.0.0", "commit": "a" * 40,
             "moodle-version": 2024042200, "supported-moodle": ["4.5"],
             "zip-sha256": "b" * 64, "published": "2026-01-01T00:00:00Z",
             "released": "2026-01-01T00:00:00Z"},
            {"version": "1.1.0", "tag": "v1.1.0", "commit": "c" * 40,
             "moodle-version": 2024042200, "supported-moodle": ["4.5"],
             "zip-sha256": "d" * 64, "published": "2026-02-01T00:00:00Z",
             "released": "2026-02-01T00:00:00Z"},
        ],
    }
    (index / "plugins" / "mod" / "mod_example.yml").write_text(yaml.safe_dump(entry))
    if revoke:
        (index / "advisories").mkdir()
        (index / "advisories" / "CAMP-2026-0001.yml").write_text(yaml.safe_dump({
            "id": "CAMP-2026-0001", "component": "mod_example",
            "title": "test", "severity": "high", "published": "2026-03-01",
            "affected": [{"versions": ["1.0.0"], "revoked": True}],
            "summary": "test", "references": []}))
    return index


def test_served_releases_match_composer_exclusions(tmp_path):
    index = _index(tmp_path)
    names = [name for _, _, name in served_releases(index)]
    assert names == ["mod_example/mod_example-1.0.0.zip", "mod_example/mod_example-1.1.0.zip"]
    assert list(served_releases(_index(tmp_path / "t1", tier=1))) == []
    assert list(served_releases(_index(tmp_path / "dl", status="delisted"))) == []


def test_ledger_targets_need_lengths(tmp_path):
    index = _index(tmp_path)
    targets, problems = ledger_targets(index, {"mod_example/mod_example-1.0.0.zip": 10})
    assert set(targets) == {"mod_example/mod_example-1.0.0.zip"}
    assert targets["mod_example/mod_example-1.0.0.zip"].hashes == {"sha256": "b" * 64}
    assert targets["mod_example/mod_example-1.0.0.zip"].length == 10
    assert problems == ["mod_example/mod_example-1.1.0.zip: no artifact length (run "
                        "`camp archive-audit --lengths-out` in the same publish)"]


@pytest.fixture
def phase2(tmp_path):
    """A dist with the site targets plus noise, keys, and a ledger."""
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in SITE_TARGETS:
        (dist / name).write_text(json.dumps({"file": name}))
    (dist / "plugin").mkdir()
    (dist / "plugin" / "mod_example.html").write_text("<html>noise</html>")
    keys = tmp_path / "keys"
    init_keys(keys, root_keys=1, threshold=1)
    index = _index(tmp_path)
    lengths = {"mod_example/mod_example-1.0.0.zip": 10,
               "mod_example/mod_example-1.1.0.zip": 11}
    (tmp_path / "lengths.json").write_text(json.dumps(lengths))
    return dist, keys, index, tmp_path / "meta", tmp_path / "lengths.json"


def test_sign_only_site_targets_plus_ledger(phase2):
    dist, keys, index, meta, lengths_file = phase2
    extra, problems = ledger_targets(index, json.loads(lengths_file.read_text()))
    assert problems == []
    versions = sign_repository(dist, keys, meta, only=SITE_TARGETS, extra=extra)
    signed = Metadata.from_file(str(meta / "targets.json")).signed
    assert set(signed.targets) == set(SITE_TARGETS) | set(extra)
    assert "plugin/mod_example.html" not in signed.targets
    assert versions["target-files"] == 5
    # consistent_snapshot layout beside the unversioned copies
    for role in ("root", "targets", "snapshot"):
        assert (meta / f"1.{role}.json").exists()
    assert not (meta / "1.timestamp.json").exists()
    # ZIPs are off-host: verify must pass when told so, fail when not
    assert verify_repository(meta, dist, missing_ok_suffixes=(".zip",)) == []
    assert any("missing on disk" in p for p in verify_repository(meta, dist))


def test_sign_only_refuses_a_missing_site_target(phase2):
    dist, keys, index, meta, _ = phase2
    (dist / "index.json").unlink()
    with pytest.raises(FileNotFoundError):
        sign_repository(dist, keys, meta, only=SITE_TARGETS)


def test_default_and_overridden_expiry(phase2):
    dist, keys, _, meta, _ = phase2
    sign_repository(dist, keys, meta, only=SITE_TARGETS)
    now = datetime.datetime.now(datetime.UTC)
    ts = Metadata.from_file(str(meta / "timestamp.json")).signed.expires
    assert abs((ts - now).days - EXPIRY_DAYS["timestamp"]) <= 1
    assert EXPIRY_DAYS["timestamp"] == 14 and EXPIRY_DAYS["snapshot"] == 30
    sign_repository(dist, keys, meta, only=SITE_TARGETS,
                    expiry=parse_expiry(["timestamp=3", "snapshot=14"]))
    ts = Metadata.from_file(str(meta / "timestamp.json")).signed.expires
    assert abs((ts - now).days - 3) <= 1


def test_parse_expiry_validates():
    assert parse_expiry(None) == {}
    assert parse_expiry(["timestamp=7"]) == {"timestamp": 7}
    with pytest.raises(ValueError):
        parse_expiry(["root=1"])
    with pytest.raises(ValueError):
        parse_expiry(["timestamp=40"])      # > default snapshot 30
    with pytest.raises(ValueError):
        parse_expiry(["snapshot=x"])


def test_cli_sign_and_verify_phase2_shape(phase2):
    dist, keys, index, meta, lengths_file = phase2
    argv = ["tuf", "sign", str(dist), str(keys), str(meta),
            "--index", str(index), "--artifact-lengths", str(lengths_file),
            "--expiry", "timestamp=14"]
    for name in SITE_TARGETS:
        argv += ["--only", name]
    assert main(argv) == 0
    assert main(["tuf", "verify", str(meta), str(dist),
                 "--missing-ok-suffix", ".zip"]) == 0
    assert main(["tuf", "verify", str(meta), str(dist)]) == 1
