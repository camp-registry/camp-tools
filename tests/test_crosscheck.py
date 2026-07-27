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
