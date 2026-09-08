"""Root ceremony host tooling, exercised with openssl standing in for the
stewards' hardware keys: the same P-256 keys and the same DER signatures
`pkcs11-tool --signature-format openssl` produces."""

import hashlib
import json
import shutil
import subprocess

import pytest

from camp.tuf_repo import init_keys, sign_repository, verify_repository
from camp.tuf_root import CeremonyError, assemble_root, build_root, describe

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None,
                                reason="openssl CLI not available")


def _steward(tmp_path, name):
    priv = tmp_path / f"{name}.key.pem"
    pub = tmp_path / f"{name}.pem"
    subprocess.run(["openssl", "ecparam", "-genkey", "-name", "prime256v1", "-noout",
                    "-out", str(priv)], check=True, capture_output=True)
    subprocess.run(["openssl", "ec", "-in", str(priv), "-pubout", "-out", str(pub)],
                   check=True, capture_output=True)
    return priv, pub


def _sign(priv, payload, out):
    # what a steward runs, minus the hardware: ECDSA over SHA-256, DER out
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(priv), "-out", str(out),
                    str(payload)], check=True, capture_output=True)
    return out


@pytest.fixture
def ceremony(tmp_path):
    keys = tmp_path / "online"
    init_keys(keys, root_keys=1, threshold=1)      # gives us online role keys
    (keys / "root-1.pem").unlink()                 # production has none of these
    stewards = {n: _steward(tmp_path, n) for n in ("ada", "bob", "cyd", "dee")}
    out = tmp_path / "ceremony"
    summary = build_root(out, {n: pub for n, (_, pub) in stewards.items()},
                         keys, threshold=3)
    return tmp_path, keys, stewards, out, summary


def test_build_writes_payload_map_and_summary(ceremony):
    _, _, stewards, out, summary = ceremony
    payload = (out / "root-payload.bin").read_bytes()
    assert summary["payload-sha256"] == hashlib.sha256(payload).hexdigest()
    assert summary["version"] == 1 and summary["root-threshold"] == 3
    names = json.loads((out / "stewards.json").read_text())
    assert set(names) == set(stewards)
    root_keys = {k["keyid"] for k in summary["roles"]["root"]["keys"]}
    assert root_keys == set(names.values())
    assert all(k["scheme"] == "ecdsa-sha2-nistp256" for k in summary["roles"]["root"]["keys"])
    for role in ("targets", "snapshot", "timestamp"):
        assert len(summary["roles"][role]["keys"]) == 1
    assert summary["signatures"] == []                 # unsigned until assemble
    assert summary["roles"]["root"]["keys"][0]["name"] in stewards


def test_three_of_four_completes_two_do_not(ceremony):
    tmp_path, _, stewards, out, _ = ceremony
    payload = out / "root-payload.bin"
    sigs = {n: _sign(priv, payload, tmp_path / f"{n}.sig")
            for n, (priv, _) in stewards.items()}
    partial = assemble_root(out, {n: sigs[n] for n in ("ada", "bob")})
    assert partial["complete"] is False and partial["new-root"]["signed"] == 2
    assert not (out / "1.root.json").exists()
    full = assemble_root(out, {n: sigs[n] for n in ("ada", "bob", "cyd")})
    assert full["complete"] is True and full["rejected"] == []
    assert (out / "1.root.json").exists()
    assert len(describe(out / "root.json")["signatures"]) == 3


def test_bad_signatures_are_named_not_counted(ceremony):
    tmp_path, _, stewards, out, _ = ceremony
    payload = out / "root-payload.bin"
    other = tmp_path / "other.bin"
    other.write_bytes(b"not the payload")
    sigs = {
        "ada": _sign(stewards["ada"][0], payload, tmp_path / "ada.sig"),
        "bob": _sign(stewards["bob"][0], other, tmp_path / "bob.sig"),        # wrong bytes
        "cyd": _sign(stewards["dee"][0], payload, tmp_path / "cyd.sig"),      # wrong key
        "eve": _sign(stewards["dee"][0], payload, tmp_path / "eve.sig"),      # not a steward
    }
    report = assemble_root(out, sigs)
    assert report["accepted"] == ["ada"]
    assert {n for n, _ in report["rejected"]} == {"bob", "cyd", "eve"}
    assert report["complete"] is False


def test_payload_must_match_root(ceremony):
    tmp_path, _, stewards, out, _ = ceremony
    (out / "root-payload.bin").write_bytes(b"tampered")
    with pytest.raises(CeremonyError, match="does not match"):
        assemble_root(out, {"ada": _sign(stewards["ada"][0], out / "root-payload.bin",
                                         tmp_path / "a.sig")})


def test_rotation_needs_old_and_new_thresholds(ceremony):
    tmp_path, keys, stewards, out, _ = ceremony
    payload = out / "root-payload.bin"
    assemble_root(out, {n: _sign(p, payload, tmp_path / f"{n}.v1.sig")
                        for n, (p, _) in stewards.items()})
    v1 = out / "1.root.json"

    # add a fifth steward, threshold stays 3
    stewards["eve"] = _steward(tmp_path, "eve")
    out2 = tmp_path / "rotation"
    summary = build_root(out2, {n: pub for n, (_, pub) in stewards.items()},
                         keys, threshold=3, previous=v1)
    assert summary["version"] == 2
    changes = describe(out2 / "root.json", out2 / "stewards.json", previous=v1)["changes-from-previous"]
    assert len(changes["root-keys-added"]) == 1 and changes["root-keys-removed"] == []
    assert changes["threshold"] == [3, 3]

    payload2 = out2 / "root-payload.bin"
    sig = lambda n: _sign(stewards[n][0], payload2, tmp_path / f"{n}.v2.sig")  # noqa: E731
    # eve + two old keys: 3 of the NEW root, but only 2 of the OLD root
    report = assemble_root(out2, {"eve": sig("eve"), "ada": sig("ada"), "bob": sig("bob")},
                           previous=v1)
    assert report["new-root"]["verified"] is True
    assert report["previous-root"]["verified"] is False
    assert report["complete"] is False
    # a third old key satisfies both
    report = assemble_root(out2, {"eve": sig("eve"), "ada": sig("ada"), "bob": sig("bob"),
                                  "cyd": sig("cyd")}, previous=v1)
    assert report["complete"] is True and (out2 / "2.root.json").exists()


def test_publish_signs_online_roles_under_the_ceremony_root(ceremony):
    tmp_path, keys, stewards, out, _ = ceremony
    payload = out / "root-payload.bin"
    assemble_root(out, {n: _sign(p, payload, tmp_path / f"{n}.sig")
                        for n, (p, _) in list(stewards.items())[:3]})
    targets = tmp_path / "targets"
    targets.mkdir()
    (targets / "a.zip").write_bytes(b"artifact")
    meta = tmp_path / "meta"
    meta.mkdir()
    shutil.copy(out / "root.json", meta / "root.json")

    versions = sign_repository(targets, keys, meta)      # keys dir has NO root-*.pem
    assert versions["root"] == 1
    assert verify_repository(meta, targets) == []
    # the steward signatures are exactly what is on disk; publish added none
    assert len(describe(meta / "root.json")["signatures"]) == 3
    sign_repository(targets, keys, meta)
    assert describe(meta / "root.json")["version"] == 1   # root never bumps in publish


def test_publish_refuses_online_keys_the_root_does_not_list(ceremony):
    tmp_path, keys, stewards, out, _ = ceremony
    assemble_root(out, {n: _sign(p, out / "root-payload.bin", tmp_path / f"{n}.sig")
                        for n, (p, _) in list(stewards.items())[:3]})
    meta = tmp_path / "meta"
    meta.mkdir()
    shutil.copy(out / "root.json", meta / "root.json")
    other = tmp_path / "other-online"
    init_keys(other, root_keys=1)
    (other / "root-1.pem").unlink()
    targets = tmp_path / "targets"
    targets.mkdir()
    with pytest.raises(ValueError, match="not the targets key"):
        sign_repository(targets, other, meta)


def test_build_validates_inputs(tmp_path):
    keys = tmp_path / "online"
    init_keys(keys)
    _, pub = _steward(tmp_path, "ada")
    with pytest.raises(CeremonyError, match="threshold"):
        build_root(tmp_path / "x", {"ada": pub}, keys, threshold=2)
    with pytest.raises(CeremonyError, match="duplicate"):
        build_root(tmp_path / "y", {"ada": pub, "ada2": pub}, keys, threshold=1)
    with pytest.raises(CeremonyError, match="ECDSA P-256"):
        build_root(tmp_path / "z", {"dev": keys / "root-1.pem"}, keys, threshold=1)
