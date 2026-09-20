"""ci-deps: what to install beside a plugin for moodle-plugin-ci (camp-tools#50)."""

import yaml

from camp import cideps
from camp.cli import main
import pytest


@pytest.fixture(autouse=True)
def _no_remote(monkeypatch):
    monkeypatch.setattr(cideps, "_stable_branches", lambda source: [])


def _index(tmp_path, entries: dict, families: dict | None = None):
    for component, entry in entries.items():
        d = tmp_path / "plugins" / component.partition("_")[0]
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{component}.yml").write_text(yaml.safe_dump(
            {"component": component, "source": f"https://github.com/o/moodle-{component}", **entry}))
    if families:
        (tmp_path / "discovery").mkdir(exist_ok=True)
        (tmp_path / "discovery" / "subplugin-families.yml").write_text(yaml.safe_dump(families))
    return tmp_path


def _rel(tag, supported, deps=None):
    r = {"version": tag, "tag": tag, "commit": "c" * 40, "supported-moodle": supported}
    if deps:
        r["dependencies"] = deps
    return r


VERSION_PHP = """<?php
$plugin->component = 'tool_murelation';
$plugin->dependencies = ['tool_mulib' => 2026091900];
"""


def test_branch_label():
    assert cideps.branch_label("MOODLE_405_STABLE") == "4.5"
    assert cideps.branch_label("MOODLE_502_STABLE") == "5.2"
    assert cideps.branch_label("4.5") == "4.5"


def test_declared_dependency_resolves_to_release_covering_branch(tmp_path):
    idx = _index(tmp_path, {"tool_mulib": {"releases": [
        _rel("v4.5.14.03", ["4.5"]), _rel("v5.0.10.03", ["5.0", "5.1", "5.2"])]}})
    res = cideps.resolve(idx, VERSION_PHP, "MOODLE_405_STABLE")
    assert [(d.component, d.ref, d.via) for d in res.deps] == [
        ("tool_mulib", "v4.5.14.03", "tool_murelation")]
    res = cideps.resolve(idx, VERSION_PHP, "MOODLE_502_STABLE")
    assert res.deps[0].ref == "v5.0.10.03"


def test_no_covering_release_prefers_stable_branch_then_newest_then_default(tmp_path):
    none = lambda source: []
    idx = _index(tmp_path, {"tool_mulib": {"releases": [_rel("v1", ["4.1"]), _rel("v2", ["4.2"])]}})
    assert cideps.resolve(idx, VERSION_PHP, "4.5", stable_branches=none).deps[0].ref == "v2"
    idx = _index(tmp_path, {"tool_mulib": {"releases": []}})
    assert cideps.resolve(idx, VERSION_PHP, "4.5", stable_branches=none).deps[0].ref is None
    asked = []
    def some(source):
        asked.append(source); return ["MOODLE_400_STABLE", "MOODLE_500_STABLE", "main"]
    assert cideps.resolve(idx, VERSION_PHP, "MOODLE_405_STABLE", stable_branches=some).deps[0].ref == "MOODLE_400_STABLE"
    assert cideps.resolve(idx, VERSION_PHP, "5.2", stable_branches=some).deps[0].ref == "MOODLE_500_STABLE"
    assert cideps.resolve(idx, VERSION_PHP, "3.9", stable_branches=some).deps[0].ref is None
    assert asked == ["https://github.com/o/moodle-tool_mulib"] * 3
    # a covering release wins without asking the remote
    idx = _index(tmp_path, {"tool_mulib": {"releases": [_rel("v3", ["4.5"])]}})
    asked.clear()
    assert cideps.resolve(idx, VERSION_PHP, "4.5", stable_branches=some).deps[0].ref == "v3" and asked == []


def test_best_stable_branch():
    names = ["MOODLE_311_STABLE", "MOODLE_400_STABLE", "MOODLE_405_STABLE", "MOODLE_500_STABLE", "main"]
    assert cideps.best_stable_branch(names, "4.5") == "MOODLE_405_STABLE"
    assert cideps.best_stable_branch(names, "4.4") == "MOODLE_400_STABLE"
    assert cideps.best_stable_branch(names, "5.2") == "MOODLE_500_STABLE"
    assert cideps.best_stable_branch(names, "3.9") is None
    assert cideps.best_stable_branch(["main"], "4.5") is None


def test_code():
    assert cideps._code("4.5") == 405 and cideps._code("5.2") == 502 and cideps._code("3.11") == 311


def test_subplugin_parent_is_added_first_and_followed_transitively(tmp_path):
    families = {"certificateelement": {"parent": "tool_certificate", "name": "Certificate elements"}}
    idx = _index(tmp_path, {
        "tool_certificate": {"releases": []},
        "tool_muprog": {"releases": [_rel("v5", ["5.0"], {"tool_mulib": 1})]},
        "tool_mulib": {"releases": [_rel("v5", ["5.0"])]},
    }, families)
    text = ("$plugin->component = 'certificateelement_muprog';\n"
            "$plugin->dependencies = ['tool_muprog' => 1, 'tool_certificate' => 1];")
    res = cideps.resolve(idx, text, "5.0")
    assert [d.component for d in res.deps] == ["tool_certificate", "tool_muprog", "tool_mulib"]
    assert [d.via for d in res.deps] == ["certificateelement_muprog", "certificateelement_muprog", "tool_muprog"]


def test_bundled_and_unlisted_are_reported_not_installed(tmp_path):
    idx = _index(tmp_path, {})
    text = ("$plugin->component = 'local_x';\n"
            "$plugin->dependencies = ['mod_quiz' => 1, 'local_nowhere' => 1];")
    res = cideps.resolve(idx, text, "4.5")
    assert res.deps == []
    assert res.bundled == ["mod_quiz"]
    assert res.unlisted == [("local_nowhere", "local_x")]


def test_core_subplugin_type_parent_is_bundled_not_installed(tmp_path):
    idx = _index(tmp_path, {})
    res = cideps.resolve(idx, "$plugin->component = 'quizaccess_x';", "4.5")
    assert res.deps == [] and res.bundled == ["mod_quiz"] and res.unlisted == []


def test_entry_level_dependencies_used_when_releases_carry_none(tmp_path):
    idx = _index(tmp_path, {
        "tool_a": {"releases": [_rel("v1", ["4.5"])], "dependencies": {"tool_b": 1}},
        "tool_b": {"releases": [_rel("v1", ["4.5"])]}})
    res = cideps.resolve(idx, "$plugin->component = 'local_x';\n$plugin->dependencies = ['tool_a' => 1];", "4.5")
    assert [d.component for d in res.deps] == ["tool_a", "tool_b"]


def test_cycles_terminate(tmp_path):
    idx = _index(tmp_path, {
        "tool_a": {"releases": [_rel("v1", ["4.5"], {"tool_b": 1})]},
        "tool_b": {"releases": [_rel("v1", ["4.5"], {"tool_a": 1})]}})
    res = cideps.resolve(idx, "$plugin->component = 'local_x';\n$plugin->dependencies = ['tool_a' => 1];", "4.5")
    assert [d.component for d in res.deps] == ["tool_a", "tool_b"]


def test_cli_tsv_and_warnings(tmp_path, capsys):
    idx = _index(tmp_path, {"tool_mulib": {"releases": [_rel("v4.5.14.03", ["4.5"])]}})
    plugin = tmp_path / "plugin"; plugin.mkdir()
    (plugin / "version.php").write_text(
        "$plugin->component = 'tool_x';\n$plugin->dependencies = ['tool_mulib' => 1, 'local_gone' => 1, 'mod_forum' => 1];")
    assert main(["ci-deps", str(idx), str(plugin), "--moodle-branch", "MOODLE_405_STABLE"]) == 0
    out, err = capsys.readouterr()
    assert out.strip() == "tool_mulib\thttps://github.com/o/moodle-tool_mulib\tv4.5.14.03\ttool_x"
    assert "::warning::local_gone (needed by tool_x) is not listed" in err
    assert "mod_forum: bundled" in err
