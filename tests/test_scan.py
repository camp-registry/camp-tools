"""Scanner parsing and acceptance logic (no network)."""

from camp.scan import COMPONENT_RE, Candidate, _entry_for, _is_gpl


def _candidate(**overrides):
    defaults = dict(full_name="o/moodle-mod_x", html_url="https://github.com/o/moodle-mod_x",
                    owner="o", description="A plugin", license_spdx="GPL-3.0",
                    stars=1, default_branch="main", archived=False)
    return Candidate(**{**defaults, **overrides})


def test_component_regex():
    assert COMPONENT_RE.search("$plugin->component = 'mod_forum';").group(1) == "mod_forum"
    assert COMPONENT_RE.search('$plugin->component = "qtype_stack" ;').group(1) == "qtype_stack"
    # legacy activity style
    assert COMPONENT_RE.search("$module->component = 'mod_old';").group(1) == "mod_old"
    # not frankenstyle
    assert COMPONENT_RE.search("$plugin->component = 'Forum';") is None


def test_gpl_family():
    assert _is_gpl("GPL-3.0")
    assert _is_gpl("GPL-2.0-or-later")
    assert _is_gpl("AGPL-3.0")
    assert not _is_gpl("MIT")
    assert not _is_gpl(None)
    assert not _is_gpl("NOASSERTION")


def test_entry_shape():
    entry = _entry_for(_candidate(), "mod_x", "2026-07-10")
    assert entry["tier"] == 0
    assert entry["releases"] == []
    assert entry["discovered"] == "2026-07-10"
    assert entry["summary"] == "A plugin"
    assert "labels" not in entry and "security-contact" not in entry


def test_entry_omits_empty_summary():
    entry = _entry_for(_candidate(description=""), "mod_x", "2026-07-10")
    assert "summary" not in entry


def test_ledger_records_and_skips(tmp_path):
    from camp.scan import (load_ledger, record_outcome, save_ledger, should_skip)
    ledger = load_ledger(tmp_path)
    assert ledger == {}

    candidate = _candidate(license_spdx="MIT")
    record_outcome(ledger, candidate, "bad-license", "license: MIT", "2026-07-10")
    save_ledger(tmp_path, ledger)

    reloaded = load_ledger(tmp_path)
    record = reloaded["o/moodle-mod_x"]
    assert record["outcome"] == "bad-license"
    assert record["detail"] == "license: MIT"
    assert record["first-seen"] == "2026-07-10"

    # within the recheck window: skipped; after it: re-evaluated
    assert should_skip(reloaded, "o/moodle-mod_x", "2026-07-20", recheck_days=30)
    assert not should_skip(reloaded, "o/moodle-mod_x", "2026-09-01", recheck_days=30)
    # unknown repos and written entries are never skipped
    assert not should_skip(reloaded, "other/repo", "2026-07-20")
    record_outcome(reloaded, candidate, "written", "listed", "2026-07-21")
    assert not should_skip(reloaded, "o/moodle-mod_x", "2026-07-22")


def test_ledger_preserves_first_seen(tmp_path):
    from camp.scan import record_outcome
    ledger = {}
    candidate = _candidate()
    record_outcome(ledger, candidate, "no-version-php", "x", "2026-01-01")
    record_outcome(ledger, candidate, "bad-license", "y", "2026-06-01")
    record = ledger["o/moodle-mod_x"]
    assert record["first-seen"] == "2026-01-01"
    assert record["last-checked"] == "2026-06-01"
    assert record["outcome"] == "bad-license"


def test_compatible_licenses_accepted():
    from camp.scan import _is_acceptable_license
    assert _is_acceptable_license("GPL-3.0")
    assert _is_acceptable_license("MIT")
    assert _is_acceptable_license("Apache-2.0")
    assert _is_acceptable_license("BSD-3-Clause")
    assert not _is_acceptable_license("NOASSERTION")
    assert not _is_acceptable_license("CC-BY-SA-4.0")
    assert not _is_acceptable_license(None)


def test_entry_records_license():
    entry = _entry_for(_candidate(license_spdx="MIT"), "mod_x", "2026-07-11")
    assert entry["license"] == "MIT"
    entry = _entry_for(_candidate(license_spdx=None), "mod_x", "2026-07-11")
    assert "license" not in entry


def test_site_shows_compatible_license_badge(index_dir, tmp_path):
    import yaml
    from camp.site import generate as site_generate
    entry_path = index_dir / "plugins" / "mod" / "mod_example.yml"
    entry = yaml.safe_load(entry_path.read_text())
    entry["license"] = "MIT"
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))

    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "MIT · GPL-compatible" in html

    # GPL-family stays unmarked
    entry["license"] = "GPL-3.0"
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "GPL-compatible" not in html


def test_classify_license_text():
    from camp.scan import classify_license_text
    gpl3 = "Preamble blah.\nGNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n..."
    assert classify_license_text(gpl3) == "GPL-3.0"
    gpl2 = "GNU GENERAL PUBLIC LICENSE\n   Version 2, June 1991"
    assert classify_license_text(gpl2) == "GPL-2.0"
    agpl = "custom header\nGNU AFFERO GENERAL PUBLIC LICENSE Version 3"
    assert classify_license_text(agpl) == "AGPL-3.0"
    mit = "MyPlugin License\n\nPermission is hereby granted, free of charge, to any person..."
    assert classify_license_text(mit) == "MIT"
    apache = "Apache License\nVersion 2.0, January 2004\nhttp://www.apache.org/licenses/"
    assert classify_license_text(apache) == "Apache-2.0"
    bsd3 = ("Redistribution and use in source and binary forms, with or without "
            "modification... Neither the name of the copyright holder...")
    assert classify_license_text(bsd3) == "BSD-3-Clause"
    bsd2 = "Redistribution and use in source and binary forms, with or without modification"
    assert classify_license_text(bsd2) == "BSD-2-Clause"
    assert classify_license_text("All rights reserved. Proprietary.") is None
    # whitespace/case robustness (reflowed text is the common NOASSERTION cause)
    assert classify_license_text("gnu   general\n\npublic  LICENSE  ...  version 3") == "GPL-3.0"


def test_name_matches_component():
    from camp.scan import _name_matches_component
    assert _name_matches_component("o/moodle-mod_googlemeet", "mod_googlemeet")
    assert _name_matches_component("trampgeek/moodle-qtype_coderunner", "qtype_coderunner")
    assert _name_matches_component("me/coderunner", "qtype_coderunner")  # short name alone
    assert _name_matches_component("x/moodle-theme_boost_union", "theme_boost_union")
    assert not _name_matches_component("onyetapp/WORDPRESS-02-onyetmpdf", "mod_ompdf")
    assert not _name_matches_component("someone/random-repo", "mod_quiz")
