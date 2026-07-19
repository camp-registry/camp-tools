"""Published security-review feed: sanitization and version-aware display."""

import json

import yaml

from camp.reviews import fetch_feed
from camp.site import generate as site_generate


def _feed(tmp_path, plugins):
    path = tmp_path / "feed.json"
    path.write_text(json.dumps({"schema_version": 1, "plugins": plugins}))
    return str(path)


def _review(version, release, grade="A", reviewed="2026-07-01",
            url="https://mdlshield.com/reviews/x"):
    return {"version": version, "release": release, "grade": grade,
            "reviewed_at": reviewed, "review_url": url}


def test_feed_sanitizes_hostile_input(tmp_path):
    feed = fetch_feed(_feed(tmp_path, {
        "mod_example": {"versions": {
            "1": [_review("1", "v1", grade="Z")],                    # bad grade
            "2": [_review("2", "v2", url="https://evil.example/r")], # off-host url
            "3": [_review("3", "<b>v3</b>" * 20)],                   # long/markup release
        }},
        "mod_other": "not a dict",
    }))
    assert "1" not in feed.get("mod_example", {})            # bad grade dropped
    assert feed["mod_example"]["2"]["review_url"] == ""      # url stripped, review kept
    assert len(feed["mod_example"]["3"]["release"]) <= 40
    assert "mod_other" not in feed


def test_feed_unreadable_or_wrong_schema_is_none(tmp_path):
    assert fetch_feed(str(tmp_path / "missing.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version": 2, "plugins": {}}')
    assert fetch_feed(str(bad)) is None


def test_review_of_current_release_rendered(index_dir, tmp_path):
    # fixture release: moodle-version 2026011500
    feed = _feed(tmp_path, {"mod_example": {"versions": {
        "2026011500": [_review("2026011500", "1.0.0", grade="A+")]}}})
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out, reviews_source=feed)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "Security review" in html
    assert "A+" in html and "mdlshield.com" in html
    assert "not the current release" not in html


def test_review_of_other_version_carries_caveat(index_dir, tmp_path):
    feed = _feed(tmp_path, {"mod_example": {"versions": {
        "2020010100": [_review("2020010100", "0.5.0", grade="B")]}}})
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out, reviews_source=feed)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "Security review" in html and "0.5.0" in html
    assert "not in the archive" in html


def test_no_feed_or_no_review_renders_nothing(index_dir, tmp_path):
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "Security review" not in html


def test_author_declared_mdlshield_badge_suppressed(index_dir, tmp_path):
    listings = tmp_path / "listings"
    listings.mkdir()
    (listings / "mod_example.yml").write_text(
        "name: Example\nsummary: s\nbadges:\n"
        "- endpoint: https://mdlshield.com/api/badge/mod_example\n")
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out, listings_dir=listings)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "Author badges" not in html          # only badge was mdlshield: row gone
