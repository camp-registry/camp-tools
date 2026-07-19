"""Incremental publish: the previously published site is the cache."""

import json

import yaml

from camp.checks import CHECKER_VERSION, run_checks
from camp.ingest import ingest_all


def _entry(entry_path):
    return yaml.safe_load(entry_path.read_text())


# ---- checks reuse ----------------------------------------------------------

def _prior_checks(tmp_path, commit, checker=CHECKER_VERSION):
    prior = tmp_path / "prior-checks"
    prior.mkdir()
    (prior / "mod_example.json").write_text(json.dumps({
        "component": "mod_example", "checked": "2026-01-01",
        "checker": checker,
        "versions": {"1.0.0": {"tag": "v1.0.0", "commit": commit,
                               "phplint": True, "errors": 0, "warnings": 3,
                               "files": 1, "rules": {"x.y": 3}}}}))
    return prior


def test_checks_reused_from_prior_publish(index_dir, entry_path, tmp_path):
    # no php/phpcs on the test host: only reuse can produce output, which
    # is exactly the property under test
    commit = _entry(entry_path)["releases"][0]["commit"]
    out = tmp_path / "checks"
    run_checks(index_dir, out, log=lambda *a: None,
               reuse=str(_prior_checks(tmp_path, commit)))
    doc = json.loads((out / "mod_example.json").read_text())
    assert doc["versions"]["1.0.0"]["warnings"] == 3
    assert doc["checker"] == CHECKER_VERSION


def test_checks_stale_commit_not_fully_reused(index_dir, entry_path, tmp_path):
    """A prior summary whose commit no longer matches the ledger is not
    treated as current; without php on the host nothing new is computable,
    so the release stays unchecked rather than wrongly reused."""
    out = tmp_path / "checks"
    run_checks(index_dir, out, log=lambda *a: None,
               reuse=str(_prior_checks(tmp_path, "f" * 40)))
    if (out / "mod_example.json").exists():
        doc = json.loads((out / "mod_example.json").read_text())
        assert doc["versions"].get("1.0.0", {}).get("commit") != \
            _entry(entry_path)["releases"][0]["commit"]


def test_checks_bumped_checker_invalidates_prior(index_dir, entry_path, tmp_path):
    commit = _entry(entry_path)["releases"][0]["commit"]
    stale = tmp_path / "stale"
    stale.mkdir()
    prior = _prior_checks(stale, commit, checker=CHECKER_VERSION + 1)
    out = tmp_path / "checks"
    run_checks(index_dir, out, log=lambda *a: None, reuse=str(prior))
    assert not (out / "mod_example.json").exists()


# ---- ingest-all reuse ------------------------------------------------------

def test_ingest_all_reuses_published_site(index_dir, entry_path, plugin_repo, tmp_path):
    # point the entry at the local fixture repo so run 1 can clone it
    entry = _entry(entry_path)
    entry["source"] = str(plugin_repo)
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))

    out1 = tmp_path / "listings1"
    ingested, reused = ingest_all(index_dir, out1, log=lambda *a: None)
    assert (ingested, reused) == (1, 0)
    manifest = json.loads((out1 / "manifest.json").read_text())
    assert manifest["components"]["mod_example"]["commit"] == entry["releases"][0]["commit"]

    # simulate the published site: listings/ + manifest under a base dir
    site = tmp_path / "published"
    (site / "listings").mkdir(parents=True)
    (site / "listings" / "mod_example.yml").write_bytes(
        (out1 / "mod_example.yml").read_bytes())
    (site / "listings" / "manifest.json").write_text(json.dumps(manifest))

    # run 2: source is now bogus — success proves no clone happened
    entry["source"] = str(tmp_path / "does-not-exist")
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))
    out2 = tmp_path / "listings2"
    ingested, reused = ingest_all(index_dir, out2, reuse=str(site),
                                  log=lambda *a: None)
    assert (ingested, reused) == (0, 1)
    assert yaml.safe_load((out2 / "mod_example.yml").read_text())["name"] == "Example Activity"
    assert json.loads((out2 / "manifest.json").read_text())["components"]["mod_example"]


def test_ingest_all_reuse_rejects_pin_mismatch(index_dir, entry_path, plugin_repo, tmp_path):
    """A published listing that no longer matches the ledger pin falls back
    to a source ingest (which here fails loudly on the bogus source) —
    reuse can never launder unpinned bytes."""
    entry = _entry(entry_path)
    entry["source"] = str(plugin_repo)
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))
    out1 = tmp_path / "listings1"
    ingest_all(index_dir, out1, log=lambda *a: None)
    manifest = json.loads((out1 / "manifest.json").read_text())

    site = tmp_path / "published"
    (site / "listings").mkdir(parents=True)
    (site / "listings" / "mod_example.yml").write_text(
        "name: Tampered\nsummary: s\nlabels: [fully-free]\n")
    (site / "listings" / "manifest.json").write_text(json.dumps(manifest))

    entry["source"] = str(tmp_path / "does-not-exist")
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))
    out2 = tmp_path / "listings2"
    logs = []
    ingested, reused = ingest_all(index_dir, out2, reuse=str(site),
                                  log=lambda m: logs.append(m))
    assert reused == 0
    assert not (out2 / "mod_example.yml").exists()
    assert any("pin" in m or "clone failed" in m for m in logs)
