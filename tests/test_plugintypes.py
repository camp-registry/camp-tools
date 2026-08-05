"""Plugin-type table: lookup API, established-family loader, generator,
drift check (camp-tools#16, #24). Lookup tests run against the committed
table — facts like quizaccess belonging to mod_quiz are stable history."""

import json

import pytest

from camp import plugintypes as pt


def test_table_committed_and_coherent():
    table = pt.load()
    assert table["branches"][0] == "3.9" and "5.2" in table["branches"]
    assert len(table["types"]) > 60
    # every core-derived prefix has a curated name and a browse category:
    # a refreshed table with a brand-new type must fail here until the
    # overlay names it — display code never invents names
    assert set(table["types"]) <= set(pt.DISPLAY_NAMES)
    assert set(table["types"]) <= set(pt.CATEGORIES)
    assert set(pt.CATEGORIES.values()) <= set(pt.CATEGORY_ORDER)


def test_core_facts():
    types = pt.load()["types"]
    assert "parent" not in types["mod"]
    assert types["quizaccess"]["parent"] == "mod_quiz"
    assert types["tiny"]["parent"] == "editor_tiny"
    assert types["factor"]["parent"] == "tool_mfa"
    # legacy families stay anchored by the branches that carried them
    assert types["assignment"]["parent"] == "mod_assignment"
    assert "5.2" not in types["assignment"]["branches"]
    assert types["tinymce"]["parent"] == "editor_tinymce"
    # deprecatedplugintypes (5.0+) is recorded, not dropped
    assert "5.2" in types["mnetservice"]["branches"]
    assert "5.2" in types["mnetservice"]["deprecated"]


ESTABLISHED = {
    "customcertelement": {"parent": "mod_customcert",
                          "name": "Certificate elements"},
    "widgettype": {"parent": "filter_poodll", "category": "Content"},
}


def test_known_prefixes_fold_established_over_core():
    core = pt.known_prefixes()
    assert "quizaccess" in core and "customcertelement" not in core
    both = pt.known_prefixes(ESTABLISHED)
    assert "customcertelement" in both and "floreamui" not in both


def test_parent_core_established_unknown():
    assert pt.parent("mod") is None
    assert pt.parent("quizaccess") == "mod_quiz"
    assert pt.parent("customcertelement", ESTABLISHED) == "mod_customcert"
    assert pt.parent("floreamui", ESTABLISHED) is None


def test_display_name_never_invents():
    assert pt.display_name("quizaccess") == "Quiz access rules"
    assert pt.display_name("customcertelement", ESTABLISHED) == \
        "Certificate elements"
    # established without a recorded name, and plain unknowns: None —
    # the display layer falls back to the raw prefix
    assert pt.display_name("widgettype", ESTABLISHED) is None
    assert pt.display_name("floreamui") is None


def test_category_explicit_inherited_default():
    assert pt.category("quizaccess") == "Activities"
    assert pt.category("widgettype", ESTABLISHED) == "Content"
    # no category in the record: inherit from the parent's type prefix
    assert pt.category("customcertelement", ESTABLISHED) == \
        pt.CATEGORIES["mod"]
    assert pt.category("floreamui") == "Other"


def test_load_established_missing_and_valid(tmp_path):
    assert pt.load_established(tmp_path) == {}
    path = tmp_path / pt.ESTABLISHED_PATH
    path.parent.mkdir(parents=True)
    path.write_text(
        "customcertelement:\n"
        "  parent: mod_customcert\n"
        "  name: Certificate elements\n"
        "  issue: https://github.com/camp-registry/camp-index/issues/1\n")
    families = pt.load_established(tmp_path)
    assert families["customcertelement"]["parent"] == "mod_customcert"


def test_load_established_malformed_raises(tmp_path):
    path = tmp_path / pt.ESTABLISHED_PATH
    path.parent.mkdir(parents=True)
    path.write_text("customcertelement: mod_customcert\n")
    with pytest.raises(ValueError):
        pt.load_established(tmp_path)
    path.write_text("- customcertelement\n")
    with pytest.raises(ValueError):
        pt.load_established(tmp_path)


# --- generator ---------------------------------------------------------------

COMPONENTS = json.dumps({
    "plugintypes": {"mod": "mod", "tool": "admin/tool"},
    "deprecatedplugintypes": {"mnetservice": "mnet/service"},
})
PLUGINS = json.dumps({"standard": {"mod": ["quiz", "forum"], "tool": ["log"]},
                      "deleted": {}})


def _fake_fetch(url):
    if url.endswith("lib/components.json"):
        return 200, COMPONENTS
    if url.endswith("lib/plugins.json"):
        return 200, PLUGINS
    if url.endswith("mod/quiz/db/subplugins.json"):
        return 200, json.dumps({"plugintypes": {
            "quiz": "mod/quiz/report", "quizaccess": "mod/quiz/accessrule"}})
    if url.endswith("admin/tool/log/db/subplugins.json"):
        # newer spelling; same type names
        return 200, json.dumps({"subplugintypes": {"logstore": "store"},
                                "plugintypes": {"logstore": "admin/tool/log/store"}})
    return 404, ""


def test_fetch_branch_probes_standard_plugins():
    found = pt.fetch_branch(500, fetch=_fake_fetch)
    assert found["mod"] == {"parent": None, "deprecated": False}
    assert found["mnetservice"]["deprecated"] is True
    assert found["quizaccess"] == {"parent": "mod_quiz", "deprecated": False}
    assert found["logstore"]["parent"] == "tool_log"
    assert "forumreport" not in found          # mod_forum probe 404s


def test_fetch_branch_raises_on_bad_probe():
    def fetch(url):
        if url.endswith("mod/forum/db/subplugins.json"):
            return 500, ""
        return _fake_fetch(url)
    with pytest.raises(RuntimeError, match="mod_forum"):
        pt.fetch_branch(500, fetch=fetch)


def test_build_table_inverts_per_prefix():
    table = pt.build_table(fetch=_fake_fetch)
    assert table["types"]["quizaccess"]["branches"] == table["branches"]
    assert table["types"]["quizaccess"]["parent"] == "mod_quiz"
    assert table["types"]["mnetservice"]["deprecated"] == table["branches"]
    assert "deprecated" not in table["types"]["mod"]
    assert list(table["types"]) == sorted(table["types"])


def test_check_plugin_types_drift(monkeypatch, capsys):
    from camp.cli import main

    monkeypatch.setattr(pt, "build_table", lambda **_: pt.load())
    assert main(["check-plugin-types"]) == 0
    assert "current with upstream" in capsys.readouterr().out

    drifted = json.loads(json.dumps(pt.load()))
    drifted["types"]["newthing"] = {"branches": ["5.2"]}
    monkeypatch.setattr(pt, "build_table", lambda **_: drifted)
    assert main(["check-plugin-types"]) == 1
    err = capsys.readouterr().err
    assert "DRIFTED" in err and "+ newthing" in err
