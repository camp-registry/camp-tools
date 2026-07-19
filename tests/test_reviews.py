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


def test_color_comes_from_their_badge_url(tmp_path):
    """MDL Shield's official hex rides in badge_url; our map is fallback only."""
    feed = fetch_feed(_feed(tmp_path, {"mod_example": {"versions": {
        "1": [dict(_review("1", "v1", grade="B+"),
                   badge_url="https://img.shields.io/badge/MDL%20Shield-B%2B-3b82f6")],
        "2": [_review("2", "v2", grade="B+")],                      # no badge_url
        "3": [dict(_review("3", "v3"), badge_url="javascript:x")],  # unparseable
    }}}))
    assert feed["mod_example"]["1"]["color"] == "#3b82f6"   # theirs, from the url
    assert feed["mod_example"]["2"]["color"] == "#3b82f6"   # fallback map, B = blue
    assert feed["mod_example"]["3"]["color"] == "#22c55e"   # fallback map, A


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
    # the badge itself links the review; the duplicate text link is gone
    assert ('<a href="https://mdlshield.com/reviews/x" class="msbadge-link">'
            '<span class="abadge">') in html
    assert "Full review" not in html
    assert "more at\nmdlshield.com" in html or "more at mdlshield.com" in html


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


def test_versions_table_has_headers(index_dir, entry_path, tmp_path, plugin_repo):
    """The table grew headers (MDL Shield feedback: the '-' column read as a
    mystery). Needs >1 release for the table to render at all."""
    import yaml
    from conftest import git
    from camp.build import build_zip, resolve_tag
    (plugin_repo / "lib.php").write_text("<?php // v2\n")
    git(plugin_repo, "add", "-A")
    git(plugin_repo, "commit", "-q", "-m", "v2")
    git(plugin_repo, "tag", "v2.0.0")
    entry = yaml.safe_load(entry_path.read_text())
    second = dict(entry["releases"][0])
    second.update({"version": "2.0.0", "tag": "v2.0.0",
                   "commit": resolve_tag(str(plugin_repo), "v2.0.0"),
                   "zip-sha256": build_zip(str(plugin_repo), "v2.0.0",
                                           "mod_example").sha256})
    second.pop("listing-sha256", None)
    entry["releases"].append(second)
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))

    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert 'class="vrow vhead"' in html
    assert ">Code check</span>" in html


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


# ---- official badge artwork ------------------------------------------------

SHIELDS_SAMPLE = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="88" height="20">'
    b'<linearGradient id="s" x2="0" y2="100%"/>'
    b'<clipPath id="r"><rect width="88" height="20" rx="3"/></clipPath>'
    b'<g clip-path="url(#r)"><rect width="63" height="20" fill="#555"/>'
    b'<rect x="63" width="25" height="20" fill="#22c55e"/></g>'
    b'<g fill="#fff" text-anchor="middle"><text x="325" y="140">MDL Shield</text>'
    b'<text x="745" y="140">A</text></g></svg>')


def test_badge_svg_sanitizer():
    from camp.reviews import sanitize_badge_svg
    assert sanitize_badge_svg(SHIELDS_SAMPLE) == SHIELDS_SAMPLE
    hostile = [
        SHIELDS_SAMPLE.replace(b"</svg>", b"<script>alert(1)</script></svg>"),
        SHIELDS_SAMPLE.replace(b"</svg>", b'<foreignObject/></svg>'),
        SHIELDS_SAMPLE.replace(b"<g ", b'<g onload="x()" '),
        SHIELDS_SAMPLE.replace(
            b"</svg>", b'<a href="https://evil.example">x</a></svg>'),
        SHIELDS_SAMPLE.replace(b'url(#r)', b'url(https://evil.example/f)'),
        b"not xml at all",
        b"<html></html>",
    ]
    for doc in hostile:
        assert sanitize_badge_svg(doc) is None, doc[:60]


def test_official_badge_selfhosted_with_fallback(index_dir, tmp_path, monkeypatch):
    import camp.reviews as reviews_mod
    feed = _feed(tmp_path, {"mod_example": {"versions": {
        "2026011500": [dict(_review("2026011500", "1.0.0"),
                            badge_url="https://img.shields.io/badge/MDL%20Shield-A-22c55e")]}}})

    # success path: fetched (stubbed), sanitized, self-hosted, <img> rendered
    monkeypatch.setattr(reviews_mod, "fetch_badge_svg",
                        lambda url: SHIELDS_SAMPLE)
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out, reviews_source=feed)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert '<img class="msbadge" src="/mdlshield/' in html
    svgs = list((out / "mdlshield").glob("*.svg"))
    assert len(svgs) == 1 and svgs[0].read_bytes() == SHIELDS_SAMPLE

    # failure path: no artwork -> camp's chip, never a broken image
    monkeypatch.setattr(reviews_mod, "fetch_badge_svg", lambda url: None)
    out2 = tmp_path / "site2"
    site_generate(index_dir, "https://repo.test", out2, reviews_source=feed)
    html2 = (out2 / "plugin" / "mod_example.html").read_text()
    assert 'src="/mdlshield/' not in html2
    assert '<span class="abadge"><span class="l">MDL Shield</span>' in html2
