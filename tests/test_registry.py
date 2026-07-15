"""Registry invariants: validation, ledger immutability, composer/site output."""

import json

import yaml

from camp.cli import main
from camp.composer import generate as composer_generate
from camp.site import generate as site_generate
from camp.validate import validate_entry


def _mutate(entry_path, fn):
    entry = yaml.safe_load(entry_path.read_text())
    fn(entry)
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))
    return entry


def test_valid_entry_passes(entry_path):
    assert validate_entry(entry_path) == []


def test_bad_component_rejected(entry_path):
    _mutate(entry_path, lambda e: e.update(component="NotValid"))
    assert validate_entry(entry_path)


def test_tier0_with_releases_rejected(entry_path):
    _mutate(entry_path, lambda e: e.update(tier=0))
    assert any("releases" in p and "empty" in p for p in validate_entry(entry_path))


def test_tier1_claimed_with_releases_rejected(entry_path):
    _mutate(entry_path, lambda e: e.update(tier=1))
    assert any("releases" in p and "empty" in p for p in validate_entry(entry_path))


def test_tier1_claimed_without_releases_valid(entry_path):
    _mutate(entry_path, lambda e: e.update(tier=1, releases=[]))
    assert validate_entry(entry_path) == []


def test_tier1_up_requires_labels_and_contact(entry_path):
    def strip(entry):
        del entry["labels"]
        del entry["security-contact"]
    _mutate(entry_path, strip)
    problems = validate_entry(entry_path)
    assert any("labels" in p for p in problems)
    assert any("security-contact" in p for p in problems)


def test_tier0_needs_no_labels(entry_path):
    def make_tier0(entry):
        entry["tier"] = 0
        entry["releases"] = []
        del entry["labels"]
        del entry["security-contact"]
    _mutate(entry_path, make_tier0)
    assert validate_entry(entry_path) == []


def test_release_flips_claimed_to_verified(entry_path, plugin_repo):
    def make_claimed(entry):
        entry["tier"] = 1
        entry["releases"] = []
    _mutate(entry_path, make_claimed)
    assert main(["release", str(entry_path), "v1.0.0",
                 "--source", str(plugin_repo), "--supported-moodle", "4.5,5.0"]) == 0
    entry = yaml.safe_load(entry_path.read_text())
    assert entry["tier"] == 2
    assert validate_entry(entry_path) == []


def test_artifacts_materialize_hash_gated_and_idempotent(index_dir, entry_path, plugin_repo, tmp_path):
    from camp.artifacts import materialize

    _mutate(entry_path, lambda e: e.update(source=str(plugin_repo)))
    out = tmp_path / "artifacts"

    result = materialize(index_dir, out)
    assert result.built == 1 and not result.problems
    entry = yaml.safe_load(entry_path.read_text())
    zip_path = out / "mod_example" / "mod_example-1.0.0.zip"
    import hashlib
    assert hashlib.sha256(zip_path.read_bytes()).hexdigest() == entry["releases"][0]["zip-sha256"]

    # Second run rebuilds nothing.
    result = materialize(index_dir, out)
    assert result.built == 0 and result.kept == 1

    # A ledger hash the rebuild can't reproduce is never shipped.
    _mutate(entry_path, lambda e: e["releases"][0].update({"zip-sha256": "f" * 64}))
    zip_path.unlink()
    result = materialize(index_dir, out)
    assert result.built == 0 and result.problems
    assert not zip_path.exists()


def test_tier_badges_emitted_for_claimed_and_up(index_dir, entry_path, tmp_path):
    from camp.site import generate as site_generate
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)   # fixture entry is tier 2
    import json
    doc = json.loads((out / "badge" / "mod_example.json").read_text())
    assert doc == {"schemaVersion": 1, "label": "camp",
                   "message": "Tier 2 \u00b7 Verified", "color": "#23854f"}
    svg = (out / "badge" / "mod_example.svg").read_text()
    assert "Tier 2" in svg and "<script" not in svg

    _mutate(entry_path, lambda e: e.update(tier=0, releases=[]))
    entry = yaml.safe_load(entry_path.read_text())
    del entry["labels"]; del entry["security-contact"]
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))
    out2 = tmp_path / "site2"
    site_generate(index_dir, "https://repo.test", out2)
    assert not (out2 / "badge" / "mod_example.svg").exists()


def test_endpoint_badge_sanitization():
    from camp.badge import sanitize_endpoint_document, allowed_endpoint
    ok = sanitize_endpoint_document(
        b'{"schemaVersion":1,"label":"MDL Shield","message":"A","color":"#22c55e"}')
    assert ok == {"label": "MDL Shield", "message": "A", "color": "#22c55e"}
    # hostile/junk inputs
    assert sanitize_endpoint_document(b"not json") is None
    assert sanitize_endpoint_document(b'{"schemaVersion":2,"label":"x","message":"y"}') is None
    evil = sanitize_endpoint_document(
        b'{"schemaVersion":1,"label":"<script>","message":"x","color":"url(javascript:1)"}')
    assert evil["color"] == "#555"          # unknown color collapses to grey
    assert evil["label"] == "<script>"      # escaped at render, kept as data
    long = sanitize_endpoint_document(
        b'{"schemaVersion":1,"label":"L","message":"' + b"m" * 500 + b'"}')
    assert len(long["message"]) <= 40
    assert allowed_endpoint("https://mdlshield.com/api/badge/x")
    assert not allowed_endpoint("https://evil.example/api/badge/x")
    assert not allowed_endpoint("http://mdlshield.com/api/badge/x")


def test_listing_badges_validated_against_allowlist(tmp_path):
    from camp.validate import validate_listing
    good = tmp_path / "good.yml"
    good.write_text(
        "name: X\nsummary: s\nlabels: [fully-free]\nbadges:\n"
        "- endpoint: https://mdlshield.com/api/badge/mod_x\n")
    assert validate_listing(good) == []
    bad = tmp_path / "bad.yml"
    bad.write_text(
        "name: X\nsummary: s\nlabels: [fully-free]\nbadges:\n"
        "- endpoint: https://evil.example/api/badge/mod_x\n")
    assert any("allowlist" in p for p in validate_listing(bad))


def test_author_badges_rendered_from_stubbed_fetch(index_dir, tmp_path, monkeypatch):
    import camp.badge as badge_mod
    from camp.site import generate as site_generate
    monkeypatch.setattr(badge_mod, "fetch_endpoint",
                        lambda url: {"label": "MDL Shield", "message": "A",
                                     "color": "#22c55e"})
    listings = tmp_path / "listings"
    listings.mkdir()
    (listings / "mod_example.yml").write_text(
        "name: Example\nsummary: s\nbadges:\n"
        "- endpoint: https://mdlshield.com/api/badge/mod_example\n"
        "  link: https://mdlshield.com/plugins/mod_example\n")
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out, listings_dir=listings)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "Author badges" in html
    assert "MDL Shield" in html and "#22c55e" in html
    assert "declared by the maintainer" in html


def test_upstream_release_and_drift_shown(index_dir, entry_path, tmp_path):
    from camp.site import generate as site_generate
    _mutate(entry_path, lambda e: e.update(metrics={
        "updated": "2026-07-01T00:00:00Z", "stars": 3, "forks": 1,
        "open-issues": 0, "archived": False, "checked": "2026-07-15",
        "latest-release": {"tag": "v9.9.9", "date": "2026-07-10T00:00:00Z"},
    }))
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "Upstream release" in html and "v9.9.9" in html
    # fixture ledger tag is v1.0.0, upstream v9.9.9 -> drift banner
    assert "not yet verified" in html

    # An OLDER upstream formal release must not warn (tags can outpace
    # releases; only genuinely newer upstream versions are drift).
    _mutate(entry_path, lambda e: e["metrics"].update(
        {"latest-release": {"tag": "v0.5.0"}}))
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "not yet verified" not in html


def test_code_check_chip_and_badge(index_dir, tmp_path):
    import json
    from camp.site import generate as site_generate
    from camp.checks import chip
    checks = tmp_path / "checks"
    checks.mkdir()
    (checks / "mod_example.json").write_text(json.dumps({
        "component": "mod_example", "checked": "2026-07-16",
        "versions": {"1.0.0": {"tag": "v1.0.0", "commit": "0" * 40,
                               "phplint": True, "errors": 0, "warnings": 7}}}))
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out, checks_dir=checks)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "Code check" in html and "0 errors · 7 warnings" in html
    badge = (out / "badge" / "mod_example-checks.svg").read_text()
    assert "camp check" in badge

    assert chip({"phplint": False}) == ("parse errors", "#e05d44")
    assert chip({"phplint": True, "errors": 3, "warnings": 1})[0] == "3 errors · 1 warnings"
    assert chip({"phplint": True, "errors": 0, "warnings": 0}) == ("clean", "#23854f")


def test_release_history_multiple_versions_and_revocation(index_dir, entry_path, tmp_path):
    from camp.site import generate as site_generate
    _mutate(entry_path, lambda e: e["releases"].append({**e["releases"][0],
        "version": "2.0.0", "tag": "v2.0.0",
        "published": "2026-03-01T00:00:00Z"}))

    adv_dir = index_dir / "advisories" / "mod_example"
    adv_dir.mkdir(parents=True)
    (adv_dir / "CAMP-2026-0001.yml").write_text(yaml.safe_dump({
        "id": "CAMP-2026-0001", "component": "mod_example",
        "title": "RCE in old version", "severity": "critical",
        "affected-versions": "<2.0.0", "revoke": True,
        "published": "2026-03-02T00:00:00Z",
        "description": "x"}))

    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    # Newest release drives the rail; both rows render; revoked row has no
    # download and says so.
    assert "mod_example-2.0.0.zip" in html
    assert "revoked" in html
    assert "mod_example-1.0.0.zip" not in html
    # Version picker data rides the page; the revoked release is excluded
    # from the selectable set.
    assert 'id="rel-data"' in html
    # The ledger appends in publication order; a backfilled OLDER version
    # must not become the rail default. 2.0.0 was appended second here but
    # is newest; reverse case: append an older 0.9.0 and recheck.
    _mutate(entry_path, lambda e: e["releases"].append({**e["releases"][0],
        "version": "0.9.0", "tag": "v0.9.0",
        "published": "2026-04-01T00:00:00Z"}))
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert 'id="zip-ver">2.0.0<' in html
    assert '"v": "2.0.0"' in html
    assert '"v": "1.0.0"' not in html


def test_screenshots_rendered_when_ingested(index_dir, tmp_path):
    from camp.site import generate as site_generate
    listings = tmp_path / "listings"
    (listings / "screenshots" / "mod_example").mkdir(parents=True)
    (listings / "screenshots" / "mod_example" / "dashboard.png").write_bytes(b"png")
    (listings / "mod_example.yml").write_text(
        "name: Example\nsummary: s\nlabels: [fully-free]\nscreenshots:\n"
        "- path: pix/dashboard.png\n  caption: The dashboard\n")
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out, listings_dir=listings)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "/shots/mod_example/dashboard.png" in html
    assert "The dashboard" in html
    assert (out / "shots" / "mod_example" / "dashboard.png").exists()

    # A declared screenshot with no ingested file renders nothing (never a
    # hotlink, never a broken frame).
    (listings / "mod_example.yml").write_text(
        "name: Example\nsummary: s\nlabels: [fully-free]\nscreenshots:\n"
        "- path: pix/missing.png\n")
    site_generate(index_dir, "https://repo.test", out, listings_dir=listings)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert 'class="shots"' not in html
    assert '/shots/' not in html


def test_maintainer_and_ledger_on_detail_page(index_dir, tmp_path):
    from camp.site import generate as site_generate
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "Maintainer" in html and "tester" in html
    assert "Source &amp; issues" in html
    assert "verification ledger" in html
    assert "Source tagged by maintainer" in html
    assert "planned — TUF signing" in html
    assert "exactly what the\n      maintainer published" in html or "exactly what the" in html


def test_misplaced_file_rejected(entry_path):
    wrong = entry_path.parent / "wrongname.yml"
    wrong.write_text(entry_path.read_text())
    assert any("belongs at" in p for p in validate_entry(wrong))


def test_ledger_check_append_only(entry_path, tmp_path):
    base = tmp_path / "base.yml"
    base.write_text(entry_path.read_text())

    # unchanged head passes
    assert main(["ledger-check", str(base), str(entry_path)]) == 0

    # appending passes
    _mutate(entry_path, lambda e: e["releases"].append({**e["releases"][0],
        "version": "1.1.0", "tag": "v1.1.0", "published": "2026-02-01T00:00:00Z"}))
    assert main(["ledger-check", str(base), str(entry_path)]) == 0

    # mutating an existing record fails
    _mutate(entry_path, lambda e: e["releases"][0].update({"zip-sha256": "f" * 64}))
    assert main(["ledger-check", str(base), str(entry_path)]) == 1


def test_moved_requires_pointer(entry_path):
    _mutate(entry_path, lambda e: e.update(status="moved"))
    assert any("moved-to" in p for p in validate_entry(entry_path))

    _mutate(entry_path, lambda e: e.update(**{"moved-to": "https://marketplace.example/mod_example"}))
    assert validate_entry(entry_path) == []


def test_moved_stays_installable_with_abandoned_pointer(index_dir, entry_path):
    _mutate(entry_path, lambda e: e.update(
        status="moved", **{"moved-to": "https://marketplace.example/mod_example"}))
    doc = composer_generate(index_dir, "https://repo.test")
    definition = doc["packages"]["tester/moodle-mod_example"]["1.0.0"]
    assert definition["abandoned"] == "https://marketplace.example/mod_example"
    assert definition["extra"]["camp"]["moved-to"] == "https://marketplace.example/mod_example"


def test_moved_page_shows_successor_notice(index_dir, entry_path, tmp_path):
    _mutate(entry_path, lambda e: e.update(
        status="moved", **{"moved-to": "https://marketplace.example/mod_example"}))
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "This plugin has moved" in html
    assert "https://marketplace.example/mod_example" in html


def test_composer_excludes_below_tier2_and_delisted(index_dir, entry_path):
    doc = composer_generate(index_dir, "https://repo.test")
    assert list(doc["packages"]) == ["tester/moodle-mod_example"]
    dist = doc["packages"]["tester/moodle-mod_example"]["1.0.0"]["dist"]
    assert dist["shasum"] == yaml.safe_load(entry_path.read_text())["releases"][0]["zip-sha256"]

    original = entry_path.read_text()
    _mutate(entry_path, lambda e: e.update(status="delisted"))
    assert composer_generate(index_dir, "https://repo.test")["packages"] == {}

    # A claimed (tier 1) listing has no verified artifact and is never served.
    entry_path.write_text(original)
    _mutate(entry_path, lambda e: e.update(tier=1, releases=[]))
    assert composer_generate(index_dir, "https://repo.test")["packages"] == {}


def test_discovered_page_shows_summary_in_about(index_dir, entry_path, tmp_path):
    _mutate(entry_path, lambda e: e.update(
        tier=0, releases=[], summary="Scans uploads with seven antiviruses."))
    entry = yaml.safe_load(entry_path.read_text())
    del entry["labels"]; del entry["security-contact"]
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))

    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert html.count("Scans uploads with seven antiviruses.") >= 2  # tagline + About
    assert "not published a listing manifest" in html


def test_site_escapes_untrusted_content(index_dir, entry_path, tmp_path):
    _mutate(entry_path, lambda e: e.update(
        tier=0, releases=[], summary='<script>alert(1)</script>'))
    entry = yaml.safe_load(entry_path.read_text())
    del entry["labels"]; del entry["security-contact"]
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))

    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_description_markdown_is_sanitized():
    from camp.site import _render_description
    html = _render_description(
        "Real **bold** and a [link](https://example.org).\n\n"
        "- item one\n- item two\n\n"
        '<script>alert(1)</script>\n\n'
        "[bad](javascript:alert(1))\n\n"
        "![tracker](https://evil.example/pixel.png)")
    assert "<strong>bold</strong>" in html
    assert '<a href="https://example.org">link</a>' in html
    assert "<li>item one</li>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    # the javascript: link is rejected by the link validator and left as
    # inert text — it must never become an href
    assert 'href="javascript:' not in html
    assert "<img" not in html
