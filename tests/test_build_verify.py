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


def _tag_with_symlinks(plugin_repo, links, extra=None):
    """Commit files plus symlinks (path -> target string) and tag v3.0.0."""
    import os

    for relpath, content in (extra or {}).items():
        path = plugin_repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for relpath, target in links.items():
        path = plugin_repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, path)
    git(plugin_repo, "add", "-A")
    git(plugin_repo, "commit", "-q", "-m", "add symlinks")
    git(plugin_repo, "tag", "v3.0.0")


def test_intree_file_symlink_materialized(plugin_repo):
    """The webpack/Vue plugin convention (camp-index#134): amd/src/x.js is
    a symlink to the bundle webpack emitted in amd/build. The ZIP gets a
    regular file with the target's bytes at the link's path."""
    bundle = "define([],function(){/* bundle */});\n"
    _tag_with_symlinks(
        plugin_repo,
        links={"amd/src/app-lazy.js": "../build/app-lazy.min.js"},
        extra={"amd/build/app-lazy.min.js": bundle})

    first = build_zip(str(plugin_repo), "v3.0.0", "mod_example")
    zf = zipfile.ZipFile(io.BytesIO(first.data))
    assert zf.read("example/amd/src/app-lazy.js").decode() == bundle
    info = zf.getinfo("example/amd/src/app-lazy.js")
    assert (info.external_attr >> 16) & 0o777 == 0o644
    assert first.sha256 == build_zip(str(plugin_repo), "v3.0.0", "mod_example").sha256


def test_symlink_executable_bit_follows_target(plugin_repo):
    import os

    script = plugin_repo / "cli" / "run.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    os.chmod(script, 0o755)
    _tag_with_symlinks(plugin_repo, links={"run.sh": "cli/run.sh"})

    artifact = build_zip(str(plugin_repo), "v3.0.0", "mod_example")
    info = zipfile.ZipFile(io.BytesIO(artifact.data)).getinfo("example/run.sh")
    assert (info.external_attr >> 16) & 0o777 == 0o755


def test_escaping_symlink_refused(plugin_repo):
    _tag_with_symlinks(plugin_repo, links={"evil.js": "../../outside.txt"})
    with pytest.raises(BuildError, match="escapes the source tree"):
        build_zip(str(plugin_repo), "v3.0.0", "mod_example")


def test_absolute_symlink_refused(plugin_repo):
    _tag_with_symlinks(plugin_repo, links={"evil.js": "/etc/hosts"})
    with pytest.raises(BuildError, match="only relative in-tree targets"):
        build_zip(str(plugin_repo), "v3.0.0", "mod_example")


def test_dangling_symlink_refused(plugin_repo):
    _tag_with_symlinks(plugin_repo, links={"gone.js": "missing.js"})
    with pytest.raises(BuildError, match="does not exist"):
        build_zip(str(plugin_repo), "v3.0.0", "mod_example")


def test_symlink_chain_refused(plugin_repo):
    _tag_with_symlinks(
        plugin_repo,
        links={"a.js": "b.js", "b.js": "real.js"},
        extra={"real.js": "// real\n"})
    with pytest.raises(BuildError, match="chains are not supported"):
        build_zip(str(plugin_repo), "v3.0.0", "mod_example")


def test_directory_symlink_refused(plugin_repo):
    _tag_with_symlinks(
        plugin_repo,
        links={"srclink": "amd"},
        extra={"amd/build/app.min.js": "// bundle\n"})
    with pytest.raises(BuildError, match="target is a directory"):
        build_zip(str(plugin_repo), "v3.0.0", "mod_example")


def test_symlink_count_capped(plugin_repo):
    links = {f"link{i}.js": "real.js" for i in range(11)}
    _tag_with_symlinks(plugin_repo, links=links, extra={"real.js": "// real\n"})
    with pytest.raises(BuildError, match="limit 10"):
        build_zip(str(plugin_repo), "v3.0.0", "mod_example")


def test_non_plugin_tree_rejected(plugin_repo):
    git(plugin_repo, "rm", "-q", "version.php")
    git(plugin_repo, "commit", "-q", "-m", "remove version.php")
    git(plugin_repo, "tag", "v2.0.0")
    with pytest.raises(BuildError, match="version.php"):
        build_zip(str(plugin_repo), "v2.0.0", "mod_example")


def test_verify_passes_on_intact_entry(plugin_repo, entry_path):
    results = verify_entry(entry_path, source_override=str(plugin_repo))
    assert len(results) == 1 and results[0].ok
    assert results[0].warnings == []


BROKEN_LISTING = """name: mod_example
summary: A perfectly good summary that will never render.
labels:
  - fully-free
# screenshots:
#   - path: .camp/screenshots/overview.png
 links:
   issues: https://example.org/issues
"""


def _retag_with_listing(plugin_repo, entry_path, listing_text):
    """Rewrite .camp/listing.yml, re-record the release at a new tag with
    hashes computed by the real build code (pin matches the new bytes)."""
    from camp.build import build_zip, file_sha256_at_commit, resolve_tag

    (plugin_repo / ".camp" / "listing.yml").write_text(listing_text)
    git(plugin_repo, "add", "-A")
    git(plugin_repo, "commit", "-q", "-m", "update listing")
    git(plugin_repo, "tag", "v1.1.0")

    entry = yaml.safe_load(entry_path.read_text())
    release = entry["releases"][0]
    release["tag"] = "v1.1.0"
    release["commit"] = resolve_tag(str(plugin_repo), "v1.1.0")
    release["zip-sha256"] = build_zip(str(plugin_repo), "v1.1.0", "mod_example").sha256
    release["listing-sha256"] = file_sha256_at_commit(
        str(plugin_repo), release["commit"], ".camp/listing.yml")
    entry_path.write_text(yaml.safe_dump(entry))


def test_verify_warns_on_unparseable_pinned_listing(plugin_repo, entry_path):
    """The pin proves the bytes, not that they parse (camp-tools#23:
    quiz_archive shipped a manifest with a stray-indented `links:` key).
    Verification must stay green — the release is fine — but say the
    listing will not render."""
    _retag_with_listing(plugin_repo, entry_path, BROKEN_LISTING)

    results = verify_entry(entry_path, source_override=str(plugin_repo))
    assert results[0].ok
    assert any("will not show its content" in w for w in results[0].warnings)


def test_verify_warns_on_schema_invalid_pinned_listing(plugin_repo, entry_path):
    """Parseable YAML that fails the listing schema gets the same warning."""
    _retag_with_listing(plugin_repo, entry_path,
                        "name: mod_example\nsummary: fine\nunknown-key: boom\n")

    results = verify_entry(entry_path, source_override=str(plugin_repo))
    assert results[0].ok
    assert any("will not show its content" in w for w in results[0].warnings)


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


def test_verify_skips_revoked_releases(plugin_repo, entry_path):
    """A revoked release is recorded history, not a live claim: it is
    withdrawn from installation and archived, so verify must not re-check
    it (camp-tools#15: a defective record whose ref moved would otherwise
    fail every future PR for the entry forever)."""
    import yaml as _yaml

    # Break the release the same way the real incident did: move the ref.
    (plugin_repo / "lib.php").write_text("<?php // changed\n")
    git(plugin_repo, "add", "-A")
    git(plugin_repo, "commit", "-q", "-m", "change")
    git(plugin_repo, "tag", "-f", "v1.0.0")

    entry = _yaml.safe_load(entry_path.read_text())
    version = entry["releases"][0]["version"]
    advisories = entry_path.parent.parent.parent / "advisories"
    advisories.mkdir()
    (advisories / "CAMP-2026-9999.yml").write_text(_yaml.safe_dump({
        "id": "CAMP-2026-9999",
        "component": "mod_example",
        "title": "Defective publication record",
        "severity": "low",
        "affected-versions": f"={version}",
        "revoke": True,
        "published": "2026-07-27",
        "description": "Record referenced a moveable ref.",
    }))

    results = verify_entry(entry_path, source_override=str(plugin_repo))
    assert results[0].ok
    assert any("revoked" in check for check in results[0].checks)


def test_verify_still_fails_moved_tag_without_revocation(plugin_repo, entry_path):
    (plugin_repo / "lib.php").write_text("<?php // changed\n")
    git(plugin_repo, "add", "-A")
    git(plugin_repo, "commit", "-q", "-m", "change")
    git(plugin_repo, "tag", "-f", "v1.0.0")
    results = verify_entry(entry_path, source_override=str(plugin_repo))
    assert not results[0].ok
