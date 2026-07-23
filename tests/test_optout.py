"""Maintainer opt-out: listing removal plus a permanent ledger marker the
discovery scan never reopens (RFC §4.4 no-questions-asked removal)."""

import yaml

from camp.scan import load_ledger, opt_out, save_ledger, should_skip


def _write_listing(index, component, tier=0, releases=None,
                   source=None):
    path = index / "plugins" / component.partition("_")[0] / f"{component}.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"component": component,
             "source": source or f"https://github.com/o/moodle-{component}",
             "tier": tier, "releases": releases or []}
    path.write_text(yaml.safe_dump(entry))
    return path


def test_opt_out_removes_listing_and_marks_ledger(tmp_path):
    path = _write_listing(tmp_path, "block_dormant")
    failed = opt_out(tmp_path, ["block_dormant"],
                     reason="camp-index#42", log=lambda *a: None)
    assert failed == []
    assert not path.exists()
    record = load_ledger(tmp_path)["o/moodle-block_dormant"]
    assert record["outcome"] == "opted-out"
    assert "camp-index#42" in record["detail"]
    assert record["component"] == "block_dormant"


def test_opted_out_repos_skip_forever(tmp_path):
    _write_listing(tmp_path, "block_dormant")
    opt_out(tmp_path, ["block_dormant"], log=lambda *a: None)
    ledger = load_ledger(tmp_path)
    # Far past any recheck window: an ordinary rejection would re-evaluate.
    assert should_skip(ledger, "o/moodle-block_dormant", "2036-01-01")
    assert should_skip(ledger, "o/moodle-block_dormant", "2036-01-01",
                       recheck_days=0)


def test_opt_out_preserves_first_seen(tmp_path):
    _write_listing(tmp_path, "block_dormant")
    ledger = {"o/moodle-block_dormant": {
        "outcome": "exists", "detail": "d",
        "first-seen": "2026-07-11", "last-checked": "2026-07-11"}}
    save_ledger(tmp_path, ledger)
    opt_out(tmp_path, ["block_dormant"], log=lambda *a: None)
    assert load_ledger(tmp_path)["o/moodle-block_dormant"]["first-seen"] == "2026-07-11"


def test_opt_out_refuses_claimed_released_and_unknown(tmp_path):
    claimed = _write_listing(tmp_path, "block_claimed", tier=1)
    released = _write_listing(tmp_path, "block_released",
                              releases=[{"version": "1.0", "commit": "c",
                                         "sha256": "s"}])
    failed = opt_out(tmp_path,
                     ["block_claimed", "block_released", "block_ghost"],
                     log=lambda *a: None)
    assert sorted(failed) == ["block_claimed", "block_ghost", "block_released"]
    assert claimed.exists() and released.exists()
    assert load_ledger(tmp_path) == {}  # nothing marked for refused requests


def test_opt_out_gitlab_source_keys_by_project_path(tmp_path):
    _write_listing(tmp_path, "mod_gl",
                   source="https://gitlab.com/group/sub/moodle-mod_gl")
    failed = opt_out(tmp_path, ["mod_gl"], log=lambda *a: None)
    assert failed == []
    assert load_ledger(tmp_path)["group/sub/moodle-mod_gl"]["outcome"] == "opted-out"
