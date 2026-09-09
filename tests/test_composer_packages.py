"""packages.json must be consumable by a real Composer (camp-index#281-283)."""

import json

import pytest
import yaml

from camp.composer import (INSTALLER_PACKAGE, composer_constraint, composer_version,
                           generate, generate_advisories, package_metadata, write)


def _only_version(index_dir, **kwargs):
    doc = generate(index_dir, "https://repo.test", **kwargs)
    (name, versions), = doc["packages"].items()
    return name, versions


def _set_version(entry_path, version):
    entry = yaml.safe_load(entry_path.read_text())
    entry["releases"][0]["version"] = version
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))


def test_requires_the_installer_that_exists_on_packagist(index_dir):
    _, versions = _only_version(index_dir)
    require = versions["1.0.0"]["require"]
    assert INSTALLER_PACKAGE == "moodle/composer-installer"
    assert require[INSTALLER_PACKAGE] == "*"
    assert "moodle/moodle-composer-installer" not in require


def test_no_shasum_because_composer_checks_sha1_only(index_dir):
    _, versions = _only_version(index_dir)
    definition = versions["1.0.0"]
    assert "shasum" not in definition["dist"]
    assert len(definition["extra"]["camp"]["zip-sha256"]) == 64
    assert definition["extra"]["camp"]["version"] == "1.0.0"


def test_release_suffix_rewritten_to_patch(index_dir, entry_path):
    _set_version(entry_path, "v5.2-r3")
    _, versions = _only_version(index_dir)
    assert list(versions) == ["v5.2-patch3"]
    definition = versions["v5.2-patch3"]
    assert definition["version"] == "v5.2-patch3"
    # the artifact was archived under the author's version string
    assert definition["dist"]["url"].endswith("/mod_example/mod_example-v5.2-r3.zip")
    assert definition["extra"]["camp"]["version"] == "v5.2-r3"


def test_unparseable_version_is_skipped_and_reported(index_dir, entry_path):
    _set_version(entry_path, "2026-summer-edition")
    skipped = []
    doc = generate(index_dir, "https://repo.test", skipped=skipped)
    assert doc["packages"] == {}
    assert skipped == ["mod_example 2026-summer-edition"]


def test_advisory_range_uses_the_same_rewrite(index_dir):
    advisories = index_dir / "advisories"
    advisories.mkdir()
    (advisories / "CAMP-2026-0001.yml").write_text(yaml.safe_dump({
        "id": "CAMP-2026-0001",
        "component": "mod_example",
        "title": "Withdrawn record",
        "severity": "low",
        "affected-versions": "=v5.2-r1",
        "fixed-in": "v5.2-r2",
        "revoke": True,
        "published": "2026-07-27T00:00:00Z",
        "description": "test",
    }, sort_keys=False))
    doc = generate_advisories(index_dir, "https://repo.test")
    (advisory,) = doc["advisories"]["tester/moodle-mod_example"]
    assert advisory["affectedVersions"] == "=v5.2-patch1"


@pytest.mark.parametrize("constraint, expected", [
    ("<5.0.3", "<5.0.3"),
    ("=v1.5.4", "=v1.5.4"),
    ("=v5.2-r1", "=v5.2-patch1"),
    (">=v5.2-r1 <v5.2-r4", ">=v5.2-patch1 <v5.2-patch4"),
    ("<1.0.0 || >=2.0.0-r2", "<1.0.0 || >=2.0.0-patch2"),
])
def test_composer_constraint_rewrite(constraint, expected):
    assert composer_constraint(constraint) == expected


@pytest.mark.parametrize("version, expected", [
    ("1.0.0", "1.0.0"),
    ("v1.64", "v1.64"),
    ("20.1", "20.1"),
    ("1.0.0-beta2", "1.0.0-beta2"),
    ("2.0.0-RC1", "2.0.0-RC1"),
    ("1.2.3+build.7", "1.2.3"),
    ("v5.2-r1", "v5.2-patch1"),
    ("5.2-r10", "5.2-patch10"),
    ("4.1.2_r2", "4.1.2-patch2"),
    ("2026.09.03", "2026.09.03"),
    ("1.2.3.4.5", None),
    ("2026-summer-edition", None),
    ("v5.2-r", None),
    ("release-1", None),
])
def test_composer_version_grammar(version, expected):
    assert composer_version(version) == expected


def _write_advisory(index_dir, **overrides):
    advisories = index_dir / "advisories"
    advisories.mkdir(exist_ok=True)
    doc = {
        "id": "CAMP-2026-0002", "component": "mod_example",
        "title": "Stored XSS in the example view", "severity": "high",
        "affected-versions": "<1.0.1", "fixed-in": "1.0.1",
        "published": "2026-08-01T00:00:00Z", "description": "test",
    }
    doc.update(overrides)
    (advisories / f"{doc['id']}.yml").write_text(yaml.safe_dump(doc, sort_keys=False))


def test_advisories_reach_composer_audit_through_v2_metadata(index_dir):
    """Composer reads a repository's advisories only under the v2 protocol,
    and a static host cannot answer the POST api-url, so packages.json
    declares metadata-url + available-packages + security-advisories.metadata
    and each package gets a /p2/ file carrying its advisories."""
    _write_advisory(index_dir)
    doc = generate(index_dir, "https://repo.test")
    (name,) = doc["packages"]
    assert doc["metadata-url"] == "/p2/%package%.json"
    assert doc["available-packages"] == [name]
    assert doc["security-advisories"] == {"metadata": True}

    files = package_metadata(doc, generate_advisories(index_dir, "https://repo.test"))
    metadata = files[f"p2/{name}.json"]
    assert isinstance(metadata["packages"][name], list)          # v2 shape
    assert metadata["packages"][name][0]["version"] == "1.0.0"
    # v2 loading keys versions by uid: present, integral, stable
    uid = doc["packages"][name]["1.0.0"]["uid"]
    assert isinstance(uid, int) and uid > 0
    assert generate(index_dir, "https://repo.test")["packages"][name]["1.0.0"]["uid"] == uid
    (advisory,) = metadata["security-advisories"]
    assert advisory["advisoryId"] == "CAMP-2026-0002"
    assert advisory["packageName"] == name
    assert advisory["affectedVersions"] == "<1.0.1"
    assert {"title", "sources", "reportedAt"} <= set(advisory)  # full advisory


def test_every_package_gets_a_metadata_file_even_without_advisories(index_dir, tmp_path):
    out = tmp_path / "dist" / "packages.json"
    assert write(index_dir, "https://repo.test", out) == 1
    doc = json.loads(out.read_text())
    (name,) = doc["packages"]
    per_package = json.loads((out.parent / "p2" / f"{name}.json").read_text())
    assert per_package["security-advisories"] == []
    assert [v["version"] for v in per_package["packages"][name]] == ["1.0.0"]
    # the whole-feed file tool_camp consumes is still written
    assert json.loads((out.parent / "security-advisories.json").read_text()) == \
        {"advisories": {}}
