"""Moodle-standard component table: classification, parsers, drift check
(camp-tools#25). Classification tests run against the committed table —
facts like mod_chat leaving core at 5.0 are stable history."""

import json

from camp import standardplugins as sp


def test_table_committed_and_coherent():
    table = sp.load()
    assert table["branches"][0] == "2.6" and "5.2" in table["branches"]
    assert len(table["components"]) > 400


def test_classify_standard_everywhere():
    assert sp.classify("theme_boost", ["4.1", "5.0"]) == ("standard",)
    assert sp.classify("mod_forum", None) == ("standard",)


def test_classify_unbundled_mid_range():
    # mod_chat: standard through 4.5, deleted from 5.0
    assert sp.classify("mod_chat", ["4.5", "5.0", "5.1"]) == \
        ("standard-until", "4.5", "5.0")
    # entirely-after-the-split ranges still say where the split was
    assert sp.classify("mod_survey", ["5.0", "5.1"]) == \
        ("standard-until", "4.5", "5.0")
    # entirely-before ranges are simply satisfied by core
    assert sp.classify("mod_chat", ["4.4", "4.5"]) == ("standard",)


def test_classify_anchors_pre_window_removals():
    # bootstrapbase left core long before any branch a plugin can declare
    # support for today; the historic branches keep its anchor
    assert sp.classify("theme_bootstrapbase", ["4.1"]) == \
        ("standard-until", "3.6", "3.7")
    assert sp.classify("block_xp", ["4.5"]) is None


def test_classify_no_range_defaults_to_newest_branch():
    assert sp.classify("theme_boost", None) == ("standard",)
    assert sp.classify("mod_chat", None)[0] == "standard-until"


def test_fetch_branch_parses_plugins_json():
    doc = json.dumps({"standard": {"mod": ["forum"], "theme": ["boost"]},
                      "deleted": {"mod": ["chat"]}})
    standard, deleted = sp.fetch_branch(500, fetch=lambda url: (200, doc))
    assert standard == {"mod_forum", "theme_boost"}
    assert deleted == {"mod_chat"}


def test_fetch_branch_parses_php_arrays():
    php = """
    public static function is_deleted_standard_plugin($type, $name) {
        $plugins = array(
            'theme' => array('bootstrapbase', 'clean'),
        );
    }
    public static function standard_plugins_list($type) {
        $standard_plugins = array(
            'mod' => array(
                'chat', 'forum'
            ),
            'theme' => array('boost'),
        );
    }
    public static function other() {}
    """

    def fetch(url):
        return (404, "") if url.endswith("plugins.json") else (200, php)

    standard, deleted = sp.fetch_branch(39, fetch=fetch)
    assert standard == {"mod_chat", "mod_forum", "theme_boost"}
    assert deleted == {"theme_bootstrapbase", "theme_clean"}


def test_build_table_inverts_per_component():
    doc = json.dumps({"standard": {"mod": ["forum"]}, "deleted": {"mod": ["chat"]}})
    table = sp.build_table(fetch=lambda url: (200, doc))
    assert table["components"]["mod_forum"]["standard"] == table["branches"]
    assert table["components"]["mod_chat"]["deleted"] == table["branches"]


def test_check_standard_plugins_drift(monkeypatch, capsys):
    from camp.cli import main

    monkeypatch.setattr(sp, "build_table", lambda **_: sp.load())
    assert main(["check-standard-plugins"]) == 0
    assert "current with upstream" in capsys.readouterr().out

    drifted = json.loads(json.dumps(sp.load()))
    drifted["components"]["mod_newthing"] = {"standard": ["5.2"]}
    monkeypatch.setattr(sp, "build_table", lambda **_: drifted)
    assert main(["check-standard-plugins"]) == 1
    err = capsys.readouterr().err
    assert "DRIFTED" in err and "+ mod_newthing" in err
