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
