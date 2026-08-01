"""Directory cross-check classification (camp-tools#14). No network; the
liveness and history probes are monkeypatched."""

import yaml

import camp.crosscheck as cc
from camp.crosscheck import _owner_alias, crosscheck


def _write_entry(index, component, source, tier=0):
    path = index / "plugins" / component.partition("_")[0] / f"{component}.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({
        "component": component, "source": source, "tier": tier,
        "maintainers": [{"github": "o"}], "releases": []}))


def test_owner_alias_heuristic():
    assert _owner_alias("kelsoncm", "moodle-by-kelsoncm")
    assert _owner_alias("Cincopa-com", "moodlecincopa")
    assert not _owner_alias("udir-moodle", "justinhunt")
    assert not _owner_alias("MFreakNL", "LdesignMedia")
    assert not _owner_alias("abc", "abcd")  # too short to trust


def test_classification(tmp_path, monkeypatch):
    _write_entry(tmp_path, "mod_match", "https://github.com/a/moodle-mod_match")
    _write_entry(tmp_path, "mod_renamed", "https://github.com/a/new-name")
    _write_entry(tmp_path, "mod_alias", "https://github.com/moodle-by-kelso/x")
    _write_entry(tmp_path, "mod_dead", "https://github.com/copy/moodle-mod_dead")
    _write_entry(tmp_path, "mod_copy", "https://github.com/copyfarm/moodle-mod_copy")
    _write_entry(tmp_path, "mod_indep", "https://github.com/other/moodle-mod_indep")
    _write_entry(tmp_path, "mod_claimed", "https://github.com/new/moodle-mod_claimed",
                 tier=2)
    pluglist = {
        "mod_match": "https://github.com/a/moodle-mod_match.git",
        "mod_renamed": "https://github.com/a/old-name",
        "mod_alias": "https://github.com/kelso/x",
        "mod_dead": "https://github.com/gone/moodle-mod_dead",
        "mod_copy": "https://github.com/author/moodle-mod_copy",
        "mod_indep": "https://github.com/author2/moodle-mod_indep",
        "mod_claimed": "https://github.com/old/moodle-mod_claimed",
        "mod_missing": "https://github.com/someone/moodle-mod_missing",
    }
    monkeypatch.setattr(cc, "_repo_alive",
                        lambda url: "gone" not in url)
    monkeypatch.setattr(cc, "_shares_history",
                        lambda a, b, token: "copyfarm" in a or "copyfarm" in b or None)

    classes = crosscheck(tmp_path, pluglist, log=lambda *a: None)
    got = {name: [row[0] for row in rows] for name, rows in classes.items() if rows}
    assert got == {
        "match": ["mod_match"],
        "same-owner": ["mod_renamed"],
        "owner-alias": ["mod_alias"],
        "directory-dead": ["mod_dead"],
        "shared-history": ["mod_copy"],
        "probe-failed": ["mod_indep"],  # history probe None both ways
        "claimed-differs": ["mod_claimed"],
        "missing": ["mod_missing"],
    }


def test_no_probe_mode_coarse(tmp_path, monkeypatch):
    _write_entry(tmp_path, "mod_x", "https://github.com/copy/moodle-mod_x")
    monkeypatch.setattr(cc, "_repo_alive",
                        lambda url: (_ for _ in ()).throw(AssertionError("probed")))
    classes = crosscheck(tmp_path, {"mod_x": "https://github.com/auth/moodle-mod_x"},
                         probe=False, log=lambda *a: None)
    assert [r[0] for r in classes["independent"]] == ["mod_x"]


def test_write_reports_omits_match(tmp_path):
    classes = {"match": [("a", "b", "c")], "independent": [("x", "y", "z")],
               "missing": []}
    cc.write_reports(classes, tmp_path / "out")
    assert not (tmp_path / "out" / "match.tsv").exists()
    assert (tmp_path / "out" / "independent.tsv").read_text() == "x\ty\tz\n"
    assert (tmp_path / "out" / "missing.tsv").read_text() == ""


def test_crosscheck_uses_env_token(tmp_path, monkeypatch):
    """The first real run probed unauthenticated (60 req/hr) because the
    token never made it from the environment into the probes; 203 of 245
    pairs landed in probe-failed. The env fallback is now load-bearing."""
    _write_entry(tmp_path, "mod_x", "https://github.com/copy/moodle-mod_x")
    seen = {}
    monkeypatch.setenv("GITHUB_TOKEN", "tok-from-env")
    monkeypatch.setattr(cc, "_repo_alive", lambda url: True)

    def spy(a, b, token):
        seen["token"] = token
        return True

    monkeypatch.setattr(cc, "_shares_history", spy)
    crosscheck(tmp_path, {"mod_x": "https://github.com/auth/moodle-mod_x"},
               log=lambda *a: None)
    assert seen["token"] == "tok-from-env"


def test_missing_splits_out_removed_by_request(tmp_path, monkeypatch):
    """A directory row whose repo carries an opted-out ledger marker is
    the author's standing removal request, never a seeding candidate."""
    from camp.scan import save_ledger
    (tmp_path / "plugins").mkdir()
    save_ledger(tmp_path, {"FMCorz/moodle-filter_gone": {
        "outcome": "opted-out", "detail": "removed at maintainer request",
        "first-seen": "2026-07-23", "last-checked": "2026-07-23"}})
    classes = crosscheck(tmp_path, {
        "filter_gone": "https://github.com/FMCorz/moodle-filter_gone",
        "filter_new": "https://github.com/other/moodle-filter_new",
    }, log=lambda *a: None)
    assert [r[0] for r in classes["removed-by-request"]] == ["filter_gone"]
    assert [r[0] for r in classes["missing"]] == ["filter_new"]


def _dep_entry(component, source, tier=0, dependencies=None, releases=None):
    entry = {"component": component, "source": source,
             "maintainers": [{"github": "o"}], "tier": tier,
             "status": "active", "releases": releases or []}
    if dependencies:
        entry["dependencies"] = dependencies
    return entry


def test_dependency_xref(tmp_path):
    import yaml
    from camp.crosscheck import dependency_xref

    plugins = tmp_path / "plugins"
    (plugins / "mod").mkdir(parents=True)
    (plugins / "quizaccess").mkdir(parents=True)

    # Tier 2 entry: newest release's declaration wins over entry level.
    releases = [
        {"version": "1.0.0", "tag": "v1", "commit": "a" * 40,
         "moodle-version": 2025010100, "supported-moodle": ["4.5"],
         "zip-sha256": "b" * 64, "published": "2025-01-01T00:00:00Z",
         "dependencies": {"mod_stale": 1}},
        {"version": "2.0.0", "tag": "v2", "commit": "c" * 40,
         "moodle-version": 2026010100, "supported-moodle": ["5.0"],
         "zip-sha256": "d" * 64, "published": "2026-01-01T00:00:00Z",
         "dependencies": {"mod_quiz": 2024042200, "local_unlisted": "any"}},
    ]
    (plugins / "quizaccess" / "quizaccess_x.yml").write_text(yaml.safe_dump(
        _dep_entry("quizaccess_x", "https://github.com/o/r", tier=2,
                   releases=releases)))
    (plugins / "mod" / "mod_quiz.yml").write_text(yaml.safe_dump(
        _dep_entry("mod_quiz", "https://github.com/o/q")))
    # Tier 0 entry-level observation is read when there is no ledger.
    (plugins / "mod" / "mod_disc.yml").write_text(yaml.safe_dump(
        _dep_entry("mod_disc", "https://github.com/o/d",
                   dependencies={"local_unlisted": "any"})))

    xref = dependency_xref(tmp_path)
    assert ("quizaccess_x", "mod_quiz", 2024042200) in xref["edges"]
    assert ("quizaccess_x", "mod_stale", 1) not in xref["edges"]
    assert ("mod_disc", "local_unlisted", "any") in xref["edges"]
    # mod_stale never appears: only the newest release's declaration counts
    assert set(xref["unlisted"]) == {"local_unlisted"}
    assert sorted(xref["unlisted"]["local_unlisted"]) == ["mod_disc", "quizaccess_x"]
    # quizaccess_x declaring mod_quiz is subplugin-parent evidence
    assert ("quizaccess_x", "mod_quiz") in xref["parents"]
    assert ("mod_disc", "local_unlisted") not in xref["parents"]


def test_dependency_xref_standard_and_unbundled_classes(tmp_path):
    """Core-bundled dependencies split out of the seeding candidates, and
    the unbundling watchlist surfaces once-standard deleted components not
    in the index, newest first (camp-tools#25)."""
    import yaml
    from camp.crosscheck import dependency_xref

    plugins = tmp_path / "plugins"
    (plugins / "theme").mkdir(parents=True)
    (plugins / "theme" / "theme_x.yml").write_text(yaml.safe_dump(
        _dep_entry("theme_x", "https://github.com/o/t", tier=0,
                   dependencies={"theme_boost": "any", "local_missing": "any"})))

    xref = dependency_xref(tmp_path)
    assert xref["standard"] == {"theme_boost": ["theme_x"]}
    assert xref["unlisted"] == {"local_missing": ["theme_x"]}
    watchlist = dict(xref["unbundled-unlisted"])
    assert watchlist.get("mod_chat") == "4.5"
    assert watchlist.get("mod_survey") == "4.5"
    # deleted-only components (left core before the window) stay off it
    assert "theme_bootstrapbase" not in watchlist
    # newest unbundlings sort first
    branches = [b for _, b in xref["unbundled-unlisted"]]
    assert branches == sorted(branches, key=lambda b: -float(b))
