"""Listing ingestion: pinned-hash enforcement and untrusted-input handling."""

import yaml

from camp.ingest import ingest_entry
from conftest import git


def test_ingest_writes_pinned_listing(plugin_repo, entry_path, tmp_path):
    out = tmp_path / "listings"
    result = ingest_entry(entry_path, str(plugin_repo), out)
    assert result.ok, result.problems
    listing = yaml.safe_load((out / "mod_example.yml").read_text())
    assert listing["name"] == "Example Activity"


def test_ingest_reads_release_commit_not_tip(plugin_repo, entry_path, tmp_path):
    """A post-release change to the listing must not leak into the published
    listing for the already-released version."""
    (plugin_repo / ".camp" / "listing.yml").write_text(
        "name: Hijacked\nsummary: changed after release\nlabels: [fully-free]\n")
    git(plugin_repo, "add", "-A")
    git(plugin_repo, "commit", "-q", "-m", "post-release edit")

    out = tmp_path / "listings"
    result = ingest_entry(entry_path, str(plugin_repo), out)
    assert result.ok
    listing = yaml.safe_load((out / "mod_example.yml").read_text())
    assert listing["name"] == "Example Activity"


def test_ingest_uses_newest_version_not_ledger_last(plugin_repo, entry_path, tmp_path):
    """A backfilled older release is appended after the newest one; ingest
    must still read the listing at the newest version's commit."""
    old_listing = "name: Old Era\nsummary: pre-camp listing\nlabels: [fully-free]\n"
    (plugin_repo / ".camp" / "listing.yml").write_text(old_listing)
    git(plugin_repo, "add", "-A")
    git(plugin_repo, "commit", "-q", "-m", "the past")
    git(plugin_repo, "tag", "v0.9.0")
    from camp.build import resolve_tag
    entry = yaml.safe_load(entry_path.read_text())
    backfill = dict(entry["releases"][0])
    backfill.update({"version": "0.9.0", "tag": "v0.9.0",
                     "commit": resolve_tag(str(plugin_repo), "v0.9.0")})
    backfill.pop("listing-sha256", None)
    backfill.pop("zip-sha256", None)
    backfill["zip-sha256"] = "0" * 64   # irrelevant to ingest
    entry["releases"].append(backfill)  # append-only: older version lands last
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))

    out = tmp_path / "listings"
    result = ingest_entry(entry_path, str(plugin_repo), out)
    assert result.ok, result.problems
    listing = yaml.safe_load((out / "mod_example.yml").read_text())
    assert listing["name"] == "Example Activity"   # v1.0.0's listing, not "Old Era"


def test_ingest_fails_on_pin_mismatch(plugin_repo, entry_path, tmp_path):
    entry = yaml.safe_load(entry_path.read_text())
    entry["releases"][0]["listing-sha256"] = "0" * 64
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))

    result = ingest_entry(entry_path, str(plugin_repo), tmp_path / "listings")
    assert not result.ok
    assert any("pins" in p for p in result.problems)


def test_ingest_rejects_invalid_listing(plugin_repo, entry_path, tmp_path):
    (plugin_repo / ".camp" / "listing.yml").write_text("name: NoSummaryOrLabels\n")
    git(plugin_repo, "add", "-A")
    git(plugin_repo, "commit", "-q", "-m", "bad listing")
    git(plugin_repo, "tag", "-f", "v1.0.0")
    # recompute entry to point at the new commit with no pin
    from camp.build import resolve_tag
    entry = yaml.safe_load(entry_path.read_text())
    entry["releases"][0]["commit"] = resolve_tag(str(plugin_repo), "v1.0.0")
    del entry["releases"][0]["listing-sha256"]
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))

    out = tmp_path / "listings"
    result = ingest_entry(entry_path, str(plugin_repo), out)
    assert not result.ok
    assert not (out / "mod_example.yml").exists()
