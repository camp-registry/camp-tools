"""packages.json must be consumable by a real Composer (camp-index#281-283)."""

import pytest
import yaml

from camp.composer import (INSTALLER_PACKAGE, composer_constraint, composer_version,
                           generate, generate_advisories)


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
