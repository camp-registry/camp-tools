"""Branch derivation from version.php declarations."""

from camp.moodleversions import branch_from_requires, branches_from_supported


def test_supported_range_expands():
    assert branches_from_supported([311, 401]) == ["3.11", "4.0", "4.1"]
    assert branches_from_supported([403, 501]) == ["4.3", "4.4", "4.5", "5.0", "5.1"]
    assert branches_from_supported([405, 405]) == ["4.5"]


def test_supported_range_invalid():
    assert branches_from_supported([405]) is None
    assert branches_from_supported([1, 2]) is None


def test_requires_maps_to_containing_branch():
    assert branch_from_requires(2023100900) == "4.3"   # exactly 4.3
    assert branch_from_requires(2023101200) == "4.3"   # a 4.3 build
    assert branch_from_requires(2024100700) == "4.5"
    assert branch_from_requires(2010000000) is None    # pre-3.9


def test_release_derives_supported_from_fixture(plugin_repo, entry_path, tmp_path):
    """End-to-end: camp release on a new tag derives from $plugin->requires
    (fixture declares requires=2024100700 -> 4.5) when no flag is passed."""
    import yaml
    from camp.cli import main
    from conftest import git

    (plugin_repo / "version.php").write_text(
        (plugin_repo / "version.php").read_text().replace("'1.0.0'", "'1.1.0'"))
    git(plugin_repo, "add", "-A")
    git(plugin_repo, "commit", "-q", "-m", "1.1.0")
    git(plugin_repo, "tag", "v1.1.0")

    assert main(["release", str(entry_path), "v1.1.0", "--source", str(plugin_repo)]) == 0
    entry = yaml.safe_load(entry_path.read_text())
    assert entry["releases"][-1]["supported-moodle"] == ["4.5"]


def test_vorder_derives_from_branches():
    from camp.moodleversions import branch_names
    from camp.site import VORDER
    assert VORDER == branch_names()
    assert "3.10" in VORDER            # the hand-copied list had dropped it


def test_check_upstream_flags_unknown_branch():
    from camp.moodleversions import check_upstream
    ls = "\n".join([
        "abc\trefs/heads/MOODLE_38_STABLE",     # below floor: ignored
        "abc\trefs/heads/MOODLE_405_STABLE",    # known
        "abc\trefs/heads/MOODLE_502_STABLE",    # known
        "abc\trefs/heads/MOODLE_504_STABLE",    # NEW (5.3 is a known pre-release row)
    ])
    findings = check_upstream(ls_remote=ls, fetch_first_code=lambda c: 2027042000,
                              main_version={})
    assert len(findings) == 1
    f = findings[0]
    assert (f["code"], f["name"], f["first"], f["kind"]) == (504, "5.4", 2027042000, "stable")
    assert '(504, "5.4", 2027042000),' in f["row"]


def test_check_upstream_current_table_is_quiet():
    from camp.moodleversions import check_upstream
    ls = "abc\trefs/heads/MOODLE_405_STABLE\nabc\trefs/heads/MOODLE_502_STABLE\n"
    assert check_upstream(ls_remote=ls) == []


# --- declared range stored on the record (camp-tools#64) --------------------

def test_effective_supported_reexpands_against_current_table(monkeypatch):
    import camp.moodleversions as mv
    release = {"supported-moodle": ["5.0", "5.1", "5.2"], "supported-range": [500, 503]}
    # a table ending at 5.2 (the day the record was published): list and
    # range agree
    monkeypatch.setattr(mv, "BRANCHES", [b for b in mv.BRANCHES if b[0] <= 502])
    assert mv.effective_supported(release) == ["5.0", "5.1", "5.2"]
    # the table gains 5.3 (at beta under camp-tools#65, or on release day):
    # the record follows without re-ingest
    monkeypatch.setattr(mv, "BRANCHES", mv.BRANCHES + [(503, "5.3", 2026102000)])
    assert mv.effective_supported(release) == ["5.0", "5.1", "5.2", "5.3"]
    # no range stored (requires-only, override, or pre-field record): list rules
    assert mv.effective_supported({"supported-moodle": ["4.5"]}) == ["4.5"]
    # a range that expands to nothing falls back to the list
    assert mv.effective_supported({"supported-moodle": ["4.5"], "supported-range": [1, 2]}) == ["4.5"]


def test_parse_supported_range():
    from camp.moodleversions import parse_supported_range
    assert parse_supported_range("[500, 503]") == [500, 503]
    assert parse_supported_range("array(405, 501)") == [405, 501]
    assert parse_supported_range("[405]") is None
    assert parse_supported_range(None) is None


def test_release_stores_declared_range(plugin_repo, entry_path, tmp_path):
    """camp release records the declared [min, max] beside the expanded list,
    but not when the author's --supported-moodle override disagrees."""
    import yaml
    from camp.cli import main
    from conftest import git

    text = (plugin_repo / "version.php").read_text().replace("'1.0.0'", "'1.2.0'")
    text += "$plugin->supported = [500, 503];\n"
    (plugin_repo / "version.php").write_text(text)
    git(plugin_repo, "add", "-A")
    git(plugin_repo, "commit", "-q", "-m", "1.2.0")
    git(plugin_repo, "tag", "v1.2.0")
    assert main(["release", str(entry_path), "v1.2.0", "--source", str(plugin_repo)]) == 0
    rec = yaml.safe_load(entry_path.read_text())["releases"][-1]
    assert rec["supported-moodle"] == ["5.0", "5.1", "5.2", "5.3"]   # 5.3 = pre-release row
    assert rec["supported-range"] == [500, 503]

    (plugin_repo / "version.php").write_text(text.replace("'1.2.0'", "'1.3.0'"))
    git(plugin_repo, "add", "-A")
    git(plugin_repo, "commit", "-q", "-m", "1.3.0")
    git(plugin_repo, "tag", "v1.3.0")
    assert main(["release", str(entry_path), "v1.3.0", "--source", str(plugin_repo),
                 "--supported-moodle", "5.1,5.2"]) == 0
    rec = yaml.safe_load(entry_path.read_text())["releases"][-1]
    assert rec["supported-moodle"] == ["5.1", "5.2"]
    assert "supported-range" not in rec


def test_backfill_supported_range(index_dir, entry_path):
    import yaml
    from camp import supportedrange

    def mutate(e):
        e["releases"][0]["commit"] = "a" * 40
        e["releases"].append(dict(e["releases"][0], version="2.0.0", tag="v2.0.0",
                                  commit="b" * 40))
        e["releases"].append(dict(e["releases"][0], version="3.0.0", tag="v3.0.0",
                                  commit="c" * 40, **{"supported-range": [405, 405]}))
    entry = yaml.safe_load(entry_path.read_text()); mutate(entry)
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))

    texts = {"a" * 40: "<?php\n$plugin->requires = 2024100700;\n",            # requires only
             "b" * 40: "<?php\n$plugin->supported = [405, 502];\n"}           # declared range
    def fake_fetch(source, commit, token=None):
        return texts.get(commit)                                             # c... already has one
    stats = supportedrange.backfill(index_dir, fetch=fake_fetch, log=lambda *a: None)
    assert stats == {"written": 1, "requires-only": 1, "already": 1, "unreachable": 0}
    recs = yaml.safe_load(entry_path.read_text())["releases"]
    assert "supported-range" not in recs[0]
    assert recs[1]["supported-range"] == [405, 502]
    assert recs[2]["supported-range"] == [405, 405]

    # the index still validates with the new field
    from camp.validate import validate_entry
    assert validate_entry(entry_path) == []

    # dry run touches nothing and reports what it would do
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))
    stats = supportedrange.backfill(index_dir, fetch=fake_fetch, dry_run=True, log=lambda *a: None)
    assert stats["written"] == 1
    assert "supported-range" not in yaml.safe_load(entry_path.read_text())["releases"][1]


# --- pre-release branches (camp-tools#65) -----------------------------------

def test_prerelease_row_is_labelled_not_renamed():
    from camp.moodleversions import PRERELEASE, branch_names, display_name, maturity
    assert "5.3" in branch_names()           # matching and tool_camp use the bare name
    assert PRERELEASE["5.3"] in ("beta", "rc")
    assert display_name("5.3") == f"5.3 ({PRERELEASE['5.3']})"
    assert display_name("5.2") == "5.2" and maturity("5.2") is None


def test_check_upstream_admits_at_beta_and_promotes_at_stable():
    import camp.moodleversions as mv
    ls = "abc\trefs/heads/MOODLE_502_STABLE\n"
    # alpha on main: nothing to do
    assert mv.check_upstream(ls_remote=ls, main_version={
        "branch": 504, "version": 2026110100, "maturity": "MATURITY_ALPHA"}) == []
    # beta on main for an unknown branch: admit as pre-release
    f = mv.check_upstream(ls_remote=ls, main_version={
        "branch": 504, "version": 2026110100, "maturity": "MATURITY_BETA"})
    assert [(x["kind"], x["code"], x["first"]) for x in f] == [("prerelease", 504, 2026110100)]
    assert 'PRERELEASE["5.4"] = "beta"' in f[0]["row"]
    # the known pre-release 5.3 gets its stable branch: promote
    f = mv.check_upstream(ls_remote=ls + "abc\trefs/heads/MOODLE_503_STABLE\n",
                          main_version={}, fetch_first_code=lambda c: 2026102000)
    assert [(x["kind"], x["code"], x["first"]) for x in f] == [("promote", 503, 2026102000)]
    assert "2026102000" in f[0]["row"] and "remove '5.3' from PRERELEASE" in f[0]["row"]
    # main at rc for a branch already in the table: quiet
    assert mv.check_upstream(ls_remote=ls, main_version={
        "branch": 503, "version": 2026100200, "maturity": "MATURITY_RC"}) == []


def test_standard_plugins_prerelease_reads_main():
    from camp.standardplugins import _branch_ref
    assert _branch_ref(502) == "MOODLE_502_STABLE"
    assert _branch_ref(503) == "main"
