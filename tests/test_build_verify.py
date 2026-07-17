"""The core trust guarantees: deterministic builds, tamper detection."""

import zipfile
import io

import pytest
import yaml

from camp.build import BuildError, build_zip, plugin_folder
from camp.verify import verify_entry
from conftest import git


def test_build_is_deterministic(plugin_repo):
    first = build_zip(str(plugin_repo), "v1.0.0", "mod_example")
    second = build_zip(str(plugin_repo), "v1.0.0", "mod_example")
    assert first.data == second.data
    assert first.sha256 == second.sha256


def test_zip_layout(plugin_repo):
    artifact = build_zip(str(plugin_repo), "v1.0.0", "mod_example")
    names = zipfile.ZipFile(io.BytesIO(artifact.data)).namelist()
    assert all(name.startswith("example/") for name in names)
    assert "example/version.php" in names
    assert names == sorted(names)


def test_plugin_folder():
    assert plugin_folder("mod_example") == "example"
    assert plugin_folder("local_ai_manager") == "ai_manager"
    with pytest.raises(BuildError):
        plugin_folder("notfrankenstyle")


def test_non_plugin_tree_rejected(plugin_repo):
    git(plugin_repo, "rm", "-q", "version.php")
    git(plugin_repo, "commit", "-q", "-m", "remove version.php")
    git(plugin_repo, "tag", "v2.0.0")
    with pytest.raises(BuildError, match="version.php"):
        build_zip(str(plugin_repo), "v2.0.0", "mod_example")


def test_verify_passes_on_intact_entry(plugin_repo, entry_path):
    results = verify_entry(entry_path, source_override=str(plugin_repo))
    assert len(results) == 1 and results[0].ok


def test_verify_detects_moved_tag(plugin_repo, entry_path):
    (plugin_repo / "lib.php").write_text("<?php // changed\n")
    git(plugin_repo, "add", "-A")
    git(plugin_repo, "commit", "-q", "-m", "change")
    git(plugin_repo, "tag", "-f", "v1.0.0")

    results = verify_entry(entry_path, source_override=str(plugin_repo))
    assert not results[0].ok
    assert any("moved" in problem for problem in results[0].problems)


def test_verify_detects_ledger_hash_tamper(plugin_repo, entry_path):
    entry = yaml.safe_load(entry_path.read_text())
    entry["releases"][0]["zip-sha256"] = "0" * 64
    entry_path.write_text(yaml.safe_dump(entry))

    results = verify_entry(entry_path, source_override=str(plugin_repo))
    assert not results[0].ok
    assert any("sha256" in problem for problem in results[0].problems)


def test_verify_detects_listing_tamper(plugin_repo, entry_path):
    entry = yaml.safe_load(entry_path.read_text())
    entry["releases"][0]["listing-sha256"] = "0" * 64
    entry_path.write_text(yaml.safe_dump(entry))

    results = verify_entry(entry_path, source_override=str(plugin_repo))
    assert not results[0].ok
    assert any("listing" in problem for problem in results[0].problems)


THIRDPARTYLIBS = """<?xml version="1.0"?>
<libraries>
    <library>
        <location>{location}</location>
        <name>Example Lib</name>
        <version>1.0</version>
        <license>MIT</license>
    </library>
</libraries>
"""


def _retag_with_thirdpartylibs(plugin_repo, entry_path, xml, extra=None):
    """Commit a thirdpartylibs.xml (and optional extra files), re-record
    the release at a new tag with hashes computed by the real build code."""
    from camp.build import build_zip, resolve_tag

    (plugin_repo / "thirdpartylibs.xml").write_text(xml)
    for relpath, content in (extra or {}).items():
        path = plugin_repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(plugin_repo, "add", "-A")
    git(plugin_repo, "commit", "-q", "-m", "declare third-party libs")
    git(plugin_repo, "tag", "v1.1.0")

    entry = yaml.safe_load(entry_path.read_text())
    release = entry["releases"][0]
    release["tag"] = "v1.1.0"
    release["commit"] = resolve_tag(str(plugin_repo), "v1.1.0")
    release["zip-sha256"] = build_zip(str(plugin_repo), "v1.1.0", "mod_example").sha256
    del release["listing-sha256"]
    entry_path.write_text(yaml.safe_dump(entry))


def test_verify_detects_declared_thirdparty_missing(plugin_repo, entry_path):
    _retag_with_thirdpartylibs(
        plugin_repo, entry_path, THIRDPARTYLIBS.format(location="vendor/composer"))

    results = verify_entry(entry_path, source_override=str(plugin_repo))
    assert not results[0].ok
    assert any("vendor/composer" in p and "not in the release" in p
               for p in results[0].problems)


def test_verify_accepts_declared_thirdparty_present(plugin_repo, entry_path):
    _retag_with_thirdpartylibs(
        plugin_repo, entry_path, THIRDPARTYLIBS.format(location="vendor/lib/"),
        extra={"vendor/lib/lib.php": "<?php // vendored\n"})

    results = verify_entry(entry_path, source_override=str(plugin_repo))
    assert results[0].ok
    assert any("thirdpartylibs" in c for c in results[0].checks)


def test_verify_detects_malformed_thirdpartylibs(plugin_repo, entry_path):
    _retag_with_thirdpartylibs(plugin_repo, entry_path, "<libraries><library>")

    results = verify_entry(entry_path, source_override=str(plugin_repo))
    assert not results[0].ok
    assert any("not well-formed" in p for p in results[0].problems)
