"""Directory-anchor map and scanner gate (camp-tools#30): the frozen
pluglist snapshot as a listing authority."""

import json

from camp import directorymap as dm


def test_map_committed_and_coherent():
    table = dm.load()
    assert len(table["components"]) > 2500
    # stored normalized to host/owner/repo, and stable facts hold
    assert table["components"]["mod_attendance"] == \
        "https://github.com/danmarsden/moodle-mod_attendance"
    assert all(u.startswith("https://") for u in table["components"].values())


def test_same_repo_normalization():
    assert dm.same_repo("https://github.com/A/B", "http://www.github.com/a/b.git")
    assert dm.same_repo("https://bitbucket.org/x/y/src/main/", "https://bitbucket.org/x/y")
    assert not dm.same_repo("https://github.com/a/b", "https://github.com/a/c")


def test_build_map_from_pluglist(tmp_path):
    doc = {"plugins": [
        {"component": "mod_x", "source": "https://github.com/o/moodle-mod_x/tree/main"},
        {"component": "mod_y", "source": "https://example.org/not-a-vcs-host"},
        {"component": "mod_z", "source": ""}]}
    p = tmp_path / "pluglist.json"
    p.write_text(json.dumps(doc))
    table = dm.build_map(str(p))
    assert table["components"] == {"mod_x": "https://github.com/o/moodle-mod_x"}


def test_directory_anchor_gate_flows_and_parks():
    import camp.scan as scan
    # unmapped component: flows
    assert scan.directory_anchor_detail("block_xp_no_such",
                                        "https://github.com/o/x") is None
    # candidate IS the mapped repo (case/scheme/deep-path insensitive): flows
    assert scan.directory_anchor_detail(
        "mod_attendance", "https://github.com/DanMarsden/moodle-mod_attendance.git") is None
    # mismatch parks with the directory evidence; canonical check refutes nothing
    detail = scan.directory_anchor_detail(
        "mod_attendance", "https://github.com/copyfarm/moodle-mod_attendance",
        resolve=lambda url, token: None)
    assert detail and "danmarsden" in detail and "camp-tools#30" in detail
    # rename tolerance: candidate is the mapped repo's canonical new home
    assert scan.directory_anchor_detail(
        "mod_attendance", "https://github.com/newhome/moodle-mod_attendance",
        resolve=lambda url, token: "https://github.com/newhome/moodle-mod_attendance") is None


def test_scan_parks_directory_mismatch(tmp_path, monkeypatch):
    """A candidate declaring a directory-mapped component from a different
    repository parks in needs-review instead of listing (camp-tools#30)."""
    import camp.scan as scan
    from helpers import _candidate
    index = tmp_path / "index"
    (index / "plugins").mkdir(parents=True)
    candidate = _candidate(full_name="copyfarm/moodle-mod_attendance",
                           html_url="https://github.com/copyfarm/moodle-mod_attendance")
    monkeypatch.setattr(scan, "_search", lambda *a, **k: ([candidate], 1))
    monkeypatch.setattr(scan, "_fetch_component",
                        lambda c, t, log=None: ("ok", "mod_attendance",
                                                "<?php // GNU General Public License version 3"))
    monkeypatch.setattr(scan, "_resolve_github_canonical", lambda url, token: None)
    results = scan.scan(index, queries=["x"], limit=1, token="fake")
    assert results[0].outcome == "needs-review"
    record = scan.load_ledger(index)["copyfarm/moodle-mod_attendance"]
    assert "old moodle.org directory published" in record["detail"]
    assert not (index / "plugins" / "mod" / "mod_attendance.yml").exists()


def test_merge_map_keeps_adds_holds_and_applies():
    """camp-tools#66: the committed map is durable evidence, so a refresh
    folds the live pluglist in rather than replacing it."""
    import camp.directorymap as dm
    current = {"source": "s", "components": {
        "mod_a": "https://github.com/x/moodle-mod_a",        # unchanged
        "mod_gone": "https://github.com/x/moodle-mod_gone",  # dropped upstream: kept
        "mod_moved": "https://github.com/old/moodle-mod_moved",
        "mod_moved2": "https://github.com/old/moodle-mod_moved2",
        "mod_case": "https://github.com/x/moodle-mod_case"}}
    fresh = {"source": "s", "components": {
        "mod_a": "https://github.com/x/moodle-mod_a",
        "mod_new": "https://gitlab.com/y/moodle-mod_new",      # added
        "mod_moved": "https://github.com/new/moodle-mod_moved",   # held
        "mod_moved2": "https://github.com/new/moodle-mod_moved2", # accepted
        "mod_case": "https://github.com/X/moodle-mod_case.git"}}  # same repo, different spelling
    merged, stats = dm.merge_map(current, fresh, accept={"mod_moved2"}, today="2026-10-02")
    c = merged["components"]
    assert stats["added"] == ["mod_new"] and c["mod_new"].endswith("/y/moodle-mod_new")
    assert stats["kept"] == ["mod_gone"] and "mod_gone" in c
    assert stats["held"] == [("mod_moved", "https://github.com/old/moodle-mod_moved",
                              "https://github.com/new/moodle-mod_moved")]
    assert c["mod_moved"] == "https://github.com/old/moodle-mod_moved"
    assert [x[0] for x in stats["applied"]] == ["mod_moved2"]
    assert c["mod_moved2"] == "https://github.com/new/moodle-mod_moved2"
    assert c["mod_case"] == "https://github.com/x/moodle-mod_case"   # not a change
    assert "2026-10-02" in merged["source"]
    assert list(c) == sorted(c)
