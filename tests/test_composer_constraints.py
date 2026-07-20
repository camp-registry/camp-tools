"""Branch compatibility as Composer conflict constraints."""

import yaml

from camp.composer import generate as composer_generate


def _set_release(entry_path, supported, published="2026-01-15T12:30:00Z"):
    entry = yaml.safe_load(entry_path.read_text())
    entry["releases"][0]["supported-moodle"] = supported
    entry["releases"][0]["published"] = published
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))


def _conflict(index_dir):
    doc = composer_generate(index_dir, "https://repo.test")
    version = next(iter(next(iter(doc["packages"].values())).values()))
    return version.get("conflict", {}).get("moodle/moodle")


def test_deliberate_stop_gets_both_bounds(index_dir, entry_path):
    # supports 4.5–5.0; 5.1 existed at publish (branched 2025-10-06) — the
    # author stopped short deliberately, so the exclusion is encoded
    _set_release(entry_path, ["4.5", "5.0"])
    assert _conflict(index_dir) == "<4.5 || >=5.1"


def test_newest_known_branch_leaves_upper_open(index_dir, entry_path):
    # supports through 5.1; 5.2 hadn't branched by this publish date —
    # unknown is not incompatible, no upper bound
    _set_release(entry_path, ["4.5", "5.0", "5.1"], published="2026-01-15T12:30:00Z")
    assert _conflict(index_dir) == "<4.5"


def test_same_range_after_next_branch_exists_still_open(index_dir, entry_path):
    # regeneration after 5.2 branched must NOT retroactively add the bound:
    # knowledge is judged at the release's publish date, not today's
    _set_release(entry_path, ["4.5", "5.0", "5.1"], published="2026-01-15T12:30:00Z")
    first = _conflict(index_dir)
    _set_release(entry_path, ["4.5", "5.0", "5.1"], published="2026-05-01T12:30:00Z")
    later = _conflict(index_dir)   # published AFTER 5.2 branched (2026-04-20)
    assert first == "<4.5"
    assert later == "<4.5 || >=5.2"


def test_single_branch_release(index_dir, entry_path):
    _set_release(entry_path, ["4.5"], published="2026-01-15T12:30:00Z")
    assert _conflict(index_dir) == "<4.5 || >=5.0"


def test_cross_major_successor(index_dir, entry_path):
    # 4.5's successor is 5.0, not 4.6 — the table ordering, not arithmetic
    _set_release(entry_path, ["4.4", "4.5"], published="2026-01-15T12:30:00Z")
    assert _conflict(index_dir) == "<4.4 || >=5.0"
