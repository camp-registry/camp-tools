"""Code-check helpers (camp checks)."""
from pathlib import Path

import json

import yaml

from camp import checks
from camp.checks import (AMD_VERSION, CODE_VERSION, _facets_of, _is_thirdparty,
                         stale_facets, thirdparty_locations)


def _plugin(tmp_path, manifest):
    root = tmp_path / "mod_x"
    root.mkdir()
    (root / "thirdpartylibs.xml").write_text(manifest)
    return root


def test_declared_locations_are_normalised(tmp_path):
    root = _plugin(tmp_path, """<?xml version="1.0"?>
<libraries>
  <library><location>vendor/league</location><name>league</name><license>MIT</license></library>
  <library><location>./amd/src/lib.js/</location><name>lib</name><license>MIT</license></library>
  <library><location>../boost/scss</location><name>outside</name><license>GPL</license></library>
  <library><location></location><name>empty</name><license>MIT</license></library>
</libraries>""")
    assert thirdparty_locations(root) == ["vendor/league", "amd/src/lib.js"]
    assert _is_thirdparty(root / "vendor/league/x.php", root, ["vendor/league"])
    assert _is_thirdparty(root / "amd/src/lib.js", root, ["amd/src/lib.js"])
    assert not _is_thirdparty(root / "vendor/leagueX/x.php", root, ["vendor/league"])
    assert not _is_thirdparty(root / "lib.php", root, ["vendor/league"])


def test_missing_or_broken_manifest_skips_nothing(tmp_path):
    root = tmp_path / "mod_y"
    root.mkdir()
    assert thirdparty_locations(root) == []
    (root / "thirdpartylibs.xml").write_text("<libraries><library>")
    assert thirdparty_locations(root) == []


# ---- facets and the budgeted refresh (camp-tools#79) -----------------------

def test_legacy_summaries_map_checker_to_facets():
    assert _facets_of({}, {"checker": 5}) == {"code": 5, "amd": AMD_VERSION}
    assert _facets_of({}, {"checker": 3}) == {"code": 3, "amd": 3}
    assert _facets_of({"facets": {"code": 5, "amd": 4}}, {"checker": 1}) == {"code": 5, "amd": 4}
    assert stale_facets(None, {}, "abc") == {"code", "amd"}
    assert stale_facets({"commit": "old"}, {}, "abc") == {"code", "amd"}
    assert stale_facets({"commit": "abc", "facets": {"code": CODE_VERSION - 1, "amd": AMD_VERSION}},
                        {}, "abc") == {"code"}
    assert stale_facets({"commit": "abc", "facets": {"code": CODE_VERSION, "amd": AMD_VERSION}},
                        {}, "abc") == set()


def _index_with_releases(tmp_path, specs):
    """specs: {component: [(version, commit), ...]} -> index dir."""
    index = tmp_path / "index"
    for component, rels in specs.items():
        d = index / "plugins" / component.split("_")[0]
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{component}.yml").write_text(yaml.safe_dump({
            "component": component, "source": f"https://github.com/u/{component}",
            "maintainers": [{"github": "u"}], "tier": 2, "status": "active",
            "releases": [{"version": v, "tag": f"v{v}", "commit": c, "moodle-version": 2024100700,
                          "supported-moodle": ["4.5"], "php-min": "8.1.0",
                          "zip-sha256": "0" * 64, "listing-sha256": "0" * 64,
                          "published": "2026-01-01T00:00:00Z"} for v, c in rels],
            "license": "GPL-3.0"}, sort_keys=False))
    (index / "advisories").mkdir(exist_ok=True)
    return index


def _fake_compute(computed):
    """Stand-in for the clone-and-run worker: records what it was asked
    for and writes current-facet summaries."""
    def fake(entry, doc, work, moodle_rig, log):
        for version, r, needed in work:
            computed.append((entry["component"], version, frozenset(needed)))
            prior = doc["versions"].get(version) or {}
            facets = dict(_facets_of(prior, doc)) if prior else {}
            facets.update({f: checks.FACETS[f] for f in needed})
            doc["versions"][version] = {**prior, "tag": r["tag"], "commit": r["commit"],
                                        "phplint": True, "errors": 0, "warnings": 0,
                                        "files": 0, "rules": {}, "facets": facets}
        return len(work)
    return fake


def test_refresh_orders_missing_before_stale_and_honours_the_budget(tmp_path, monkeypatch):
    index = _index_with_releases(tmp_path, {
        "mod_a": [("1.0", "a" * 40)], "mod_b": [("1.0", "b" * 40), ("1.1", "c" * 40)]})
    store = index / "checks"
    store.mkdir()
    (store / "mod_a.json").write_text(json.dumps({   # stale code facet, checked long ago
        "component": "mod_a", "checker": CODE_VERSION - 1, "checked": "2026-01-01",
        "versions": {"1.0": {"tag": "v1.0", "commit": "a" * 40, "phplint": True,
                             "errors": 1, "warnings": 0, "files": 1, "rules": {}}}}))
    computed = []
    monkeypatch.setattr(checks, "_compute_versions", _fake_compute(computed))
    monkeypatch.setattr(checks.shutil, "which", lambda name: "/usr/bin/" + name)
    stats = checks.refresh(index, log=lambda *a: None, budget=2)
    # the two missing mod_b releases go first; the stale mod_a waits
    assert computed == [("mod_b", "1.0", frozenset({"code", "amd"})),
                        ("mod_b", "1.1", frozenset({"code", "amd"}))]
    assert stats == {"computed": 2, "imported": 0, "pending": 1, "removed": 0}
    stats = checks.refresh(index, log=lambda *a: None, budget=2)
    assert computed[-1] == ("mod_a", "1.0", frozenset({"code"}))   # only the stale facet
    assert stats["pending"] == 0
    doc = json.loads((store / "mod_a.json").read_text())
    assert doc["versions"]["1.0"]["facets"] == {"amd": AMD_VERSION, "code": CODE_VERSION}
    assert doc["checker"] == CODE_VERSION
    # a third run has nothing to do
    assert checks.refresh(index, log=lambda *a: None)["computed"] == 0


def test_publish_reads_the_store_and_computes_only_new_releases(tmp_path, monkeypatch):
    index = _index_with_releases(tmp_path, {"mod_a": [("1.0", "a" * 40), ("2.0", "d" * 40)]})
    store = index / "checks"
    store.mkdir()
    (store / "mod_a.json").write_text(json.dumps({
        "component": "mod_a", "checker": CODE_VERSION - 1,
        "versions": {"1.0": {"tag": "v1.0", "commit": "a" * 40, "phplint": True,
                             "errors": 7, "warnings": 0, "files": 1, "rules": {}}}}))
    computed = []
    monkeypatch.setattr(checks, "_compute_versions", _fake_compute(computed))
    monkeypatch.setattr(checks.shutil, "which", lambda name: "/usr/bin/" + name)
    out = tmp_path / "dist-checks"
    checks.run_checks(index, out, log=lambda *a: None)
    assert computed == [("mod_a", "2.0", frozenset({"code", "amd"}))]   # not the stale 1.0
    doc = json.loads((out / "mod_a.json").read_text())
    assert doc["versions"]["1.0"]["errors"] == 7            # served as stored
    assert doc["versions"]["2.0"]["facets"]["code"] == CODE_VERSION
    assert not (store / "mod_a.json").read_text().count('"2.0"')   # publish never writes the store


def test_refresh_removes_documents_for_entries_without_releases(tmp_path, monkeypatch):
    index = _index_with_releases(tmp_path, {"mod_a": [("1.0", "a" * 40)]})
    store = index / "checks"
    store.mkdir()
    (store / "mod_gone.json").write_text(json.dumps({"component": "mod_gone", "versions": {}}))
    monkeypatch.setattr(checks, "_compute_versions", _fake_compute([]))
    monkeypatch.setattr(checks.shutil, "which", lambda name: "/usr/bin/" + name)
    stats = checks.refresh(index, log=lambda *a: None)
    assert stats["removed"] == 1 and not (store / "mod_gone.json").exists()
