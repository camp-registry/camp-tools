"""Plugin release maturity: records, Composer suffix, defaults (camp-tools#67)."""
import json

import yaml

from camp import maturity
from camp.composer import composer_version


def test_parse_declared_constants_and_numbers():
    assert maturity.parse_declared("MATURITY_BETA") == "beta"
    assert maturity.parse_declared("MATURITY_STABLE") == "stable"
    assert maturity.parse_declared("150") == "rc"
    assert maturity.parse_declared(None) is None
    assert maturity.parse_declared("MATURITY_WHATEVER") is None


def test_parse_release_string():
    assert maturity.parse_release_string("1.3.0beta2") == ("1.3.0", "beta", 2)
    assert maturity.parse_release_string("v1.3.0-RC") == ("v1.3.0", "rc", None)
    assert maturity.parse_release_string("1.3.0 (Build: 2026)") == ("1.3.0", None, None)
    assert maturity.parse_release_string("2.0.0a1") == ("2.0.0", "alpha", 1)
    assert maturity.parse_release_string("1.3.0") == ("1.3.0", None, None)


def test_ordinal_from_string_else_publication_order():
    ledger = [
        {"version": "1.3.0", "tag": "v1.3.0-b1", "maturity": "beta"},
        {"version": "1.3.0", "tag": "v1.3.0-b2", "maturity": "beta"},
        {"version": "1.3.0", "tag": "v1.3.0-rc1", "maturity": "rc"},
        {"version": "1.3.0", "tag": "v1.3.0"},                       # stable
        {"version": "1.4.0beta3", "tag": "v1.4.0b3", "maturity": "beta"},
        {"version": "1.4.0", "tag": "v1.4.0-b", "maturity": "beta"},
    ]
    assert maturity.ordinal(ledger[0], ledger) == 1
    assert maturity.ordinal(ledger[1], ledger) == 2
    assert maturity.ordinal(ledger[2], ledger) == 1          # rc counts separately
    assert maturity.ordinal(ledger[4], ledger) == 3          # explicit in the string
    assert maturity.ordinal(ledger[5], ledger) == 1          # explicit ones don't consume ordinals
    assert maturity.label(ledger[1], ledger) == "beta 2"
    assert maturity.label(ledger[3], ledger) == ""


def test_composer_version_for_prerelease_gets_suffix():
    ledger = [
        {"version": "1.3.0", "tag": "a", "maturity": "beta"},
        {"version": "1.3.0", "tag": "b", "maturity": "beta"},
        {"version": "1.3.0", "tag": "c"},
        {"version": "1.3.0rc1", "tag": "d", "maturity": "rc"},
        {"version": "1.3.0beta", "tag": "e"},                      # string says beta, maturity stable
    ]
    cv = lambda r: maturity.composer_version_for(r, ledger, composer_version)
    assert cv(ledger[0]) == "1.3.0-beta1"
    assert cv(ledger[1]) == "1.3.0-beta2"
    assert cv(ledger[2]) == "1.3.0"
    assert cv(ledger[3]) == "1.3.0-RC1"
    assert cv(ledger[4]) == "1.3.0beta"                        # author's string, as before


def test_mismatch_notice_only_on_disagreement():
    assert maturity.mismatch_notice("beta", "1.3.0beta1") is None
    assert maturity.mismatch_notice(None, "1.3.0") is None
    assert "reads as beta" in maturity.mismatch_notice(None, "1.3.0beta1")
    assert "reads as rc" in maturity.mismatch_notice("stable", "1.3.0-RC2")
    assert maturity.mismatch_notice("beta", "1.3.0") is None   # silent string: no notice


def test_release_records_maturity_and_warns(plugin_repo, entry_path, capsys):
    from camp.cli import main
    from conftest import git

    text = (plugin_repo / "version.php").read_text().replace("'1.0.0'", "'1.5.0beta'")
    text += "$plugin->maturity = MATURITY_STABLE;\n"          # disagrees with the string
    (plugin_repo / "version.php").write_text(text)
    git(plugin_repo, "add", "-A")
    git(plugin_repo, "commit", "-q", "-m", "1.5.0beta")
    git(plugin_repo, "tag", "v1.5.0-beta")
    assert main(["release", str(entry_path), "v1.5.0-beta", "--source", str(plugin_repo)]) == 0
    assert "reads as beta" in capsys.readouterr().err
    rec = yaml.safe_load(entry_path.read_text())["releases"][-1]
    assert rec["maturity"] == "stable"                         # the record follows maturity

    text = text.replace("'1.5.0beta'", "'1.6.0'").replace("MATURITY_STABLE", "MATURITY_BETA")
    (plugin_repo / "version.php").write_text(text)
    git(plugin_repo, "add", "-A")
    git(plugin_repo, "commit", "-q", "-m", "1.6.0 beta")
    git(plugin_repo, "tag", "v1.6.0-b1")
    assert main(["release", str(entry_path), "v1.6.0-b1", "--source", str(plugin_repo)]) == 0
    rec = yaml.safe_load(entry_path.read_text())["releases"][-1]
    assert rec["maturity"] == "beta"
    from camp.validate import validate_entry
    assert validate_entry(entry_path) == []


def _add_beta(entry_path, version="1.1.0", tag="v1.1.0-b1", ordinal_in_string=False):
    entry = yaml.safe_load(entry_path.read_text())
    rec = dict(entry["releases"][0], version=version if not ordinal_in_string else version + "beta7",
               tag=tag, commit="b" * 40, maturity="beta",
               published="2026-09-01T00:00:00Z", released="2026-09-01T00:00:00Z")
    entry["releases"].append(rec)
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))


def test_packages_json_suffix_and_maturity(index_dir, entry_path):
    from camp.composer import generate
    _add_beta(entry_path)
    doc = generate(index_dir, "https://repo.test")
    (name, versions), = doc["packages"].items()
    assert set(versions) == {"1.0.0", "1.1.0-beta1"}
    assert versions["1.1.0-beta1"]["extra"]["camp"]["maturity"] == "beta"
    assert versions["1.0.0"]["extra"]["camp"]["maturity"] == "stable"


def test_site_defaults_to_newest_stable_with_optin(index_dir, entry_path, tmp_path):
    from camp.site import generate as site_generate
    from camp.validate import newest_release, newest_stable_release
    _add_beta(entry_path)
    entry = yaml.safe_load(entry_path.read_text())
    assert newest_release(entry)["version"] == "1.1.0"
    assert newest_stable_release(entry)["version"] == "1.0.0"
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert '<span class="inst-ver" id="zip-ver">1.0.0</span>' in html   # card = newest stable
    assert 'id="prerel"' in html                                        # opt-in offered
    assert "1.1.0 (beta 1)" in html                                     # history row labelled
    rel = json.loads(html.split('<script id="rel-data" type="application/json">')[1].split("</script>")[0])
    assert {r["v"]: r["m"] for r in rel["releases"]} == {"1.0.0": "stable", "1.1.0": "beta"}

    # no pre-release at all: no toggle rendered
    entry["releases"] = entry["releases"][:1]
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))
    site_generate(index_dir, "https://repo.test", out)
    assert 'id="prerel"' not in (out / "plugin" / "mod_example.html").read_text()


def test_backfill_maturity(index_dir, entry_path):
    from camp import supportedrange
    texts = {("a" * 40): "<?php\n$plugin->maturity = MATURITY_RC;\n",
             ("c" * 40): "<?php\n$plugin->requires = 2024100700;\n"}
    entry = yaml.safe_load(entry_path.read_text())
    entry["releases"][0]["commit"] = "a" * 40
    entry["releases"].append(dict(entry["releases"][0], version="2.0.0", tag="v2", commit="c" * 40))
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))
    stats = supportedrange.backfill(index_dir, fetch=lambda s, c, t=None: texts.get(c),
                                    field="maturity", log=lambda *a: None)
    assert stats == {"written": 1, "requires-only": 1, "already": 0, "unreachable": 0}
    recs = yaml.safe_load(entry_path.read_text())["releases"]
    assert recs[0]["maturity"] == "rc" and "maturity" not in recs[1]
