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
