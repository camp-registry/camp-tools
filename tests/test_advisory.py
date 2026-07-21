"""Advisory pipeline: version matching, revocation, downstream effects."""

import yaml

from camp.advisory import AdvisorySet, next_id, validate_advisory, version_matches
from camp.composer import generate as composer_generate
from camp.site import generate as site_generate


def test_version_matching():
    assert version_matches("1.2.0", ">=1.0,<1.4.2")
    assert version_matches("1.4.1", ">=1.0,<1.4.2")
    assert not version_matches("1.4.2", ">=1.0,<1.4.2")
    assert not version_matches("0.9", ">=1.0,<1.4.2")
    assert version_matches("2.0", "<=2.0")
    assert version_matches("1.63", "=1.63")
    assert version_matches("v1.63", "=1.63")  # tag-style prefix ignored
    assert version_matches("1.10.0", ">1.9")  # numeric, not lexicographic


def _write_advisory(index_dir, component="mod_example", revoke=False,
                    affected="<=1.0.0", advisory_id="CAMP-2026-0001"):
    path = index_dir / "advisories" / f"{advisory_id}.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    advisory = {
        "id": advisory_id,
        "component": component,
        "title": "SQL injection in report page",
        "severity": "high",
        "affected-versions": affected,
        "fixed-in": "1.1.0",
        "revoke": revoke,
        "published": "2026-07-11T00:00:00Z",
        "description": "Update immediately.",
    }
    with open(path, "w") as f:
        yaml.safe_dump(advisory, f, sort_keys=False)
    return path


def test_advisory_validates(index_dir):
    path = _write_advisory(index_dir)
    assert validate_advisory(path, index_dir=index_dir) == []


def test_advisory_unknown_component_fails(index_dir):
    path = _write_advisory(index_dir, component="mod_ghost")
    problems = validate_advisory(path, index_dir=index_dir)
    assert any("not in the index" in p for p in problems)


def test_advisory_misplaced_file_fails(index_dir, tmp_path):
    path = _write_advisory(index_dir)
    stray = tmp_path / "CAMP-2026-0001.yml"
    stray.write_text(path.read_text())
    assert any("belongs at" in p for p in validate_advisory(stray))


def test_advisory_old_percomponent_layout_fails(index_dir):
    # pre-0.2.21 layout: advisories/<component>/<id>.yml is now misplaced
    path = _write_advisory(index_dir)
    old = index_dir / "advisories" / "mod_example" / "CAMP-2026-0001.yml"
    old.parent.mkdir(parents=True)
    old.write_text(path.read_text())
    assert any("belongs at" in p for p in validate_advisory(old))


def test_revocation_removes_version_from_composer(index_dir):
    before = composer_generate(index_dir, "https://repo.test")
    assert "1.0.0" in before["packages"]["tester/moodle-mod_example"]

    _write_advisory(index_dir, revoke=True)
    after = composer_generate(index_dir, "https://repo.test")
    assert "tester/moodle-mod_example" not in after["packages"]


def test_non_revoking_advisory_keeps_version_but_advertises(index_dir):
    from camp.composer import generate_advisories
    _write_advisory(index_dir, revoke=False)

    packages = composer_generate(index_dir, "https://repo.test")
    assert "1.0.0" in packages["packages"]["tester/moodle-mod_example"]

    advisories = generate_advisories(index_dir, "https://repo.test")
    published = advisories["advisories"]["tester/moodle-mod_example"]
    assert published[0]["advisoryId"] == "CAMP-2026-0001"
    assert published[0]["affectedVersions"] == "<=1.0.0"


def test_site_shows_advisory(index_dir, tmp_path):
    _write_advisory(index_dir)
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "CAMP-2026-0001" in html
    assert "SQL injection in report page" in html
    assert "No published advisories" not in html
    assert 'id="advisories"' in html
    # whole card is a severity-classed link to the permalink
    assert 'class="adv open sev-high"' in html
    assert 'href="/advisories/CAMP-2026-0001.html"' in html


def test_site_advisory_follows_versions(index_dir, tmp_path):
    """The current release affected → full card + install-card line + row
    marker; a later fixed release → reassurance card with permalinks."""
    import yaml as yaml_mod
    _write_advisory(index_dir)  # affects <=1.0.0; 1.0.0 is current
    out = tmp_path / "site-active"
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert 'class="adv open sev-high"' in html          # active card
    assert 'id="adv-line"' in html and "adv-flag" in html
    assert 'class="vrow rel-row vrow-adv"' not in html  # single release: no rows table

    # append a fixed 1.1.0 release (registry data only; site rendering
    # does not re-verify hashes)
    entry_path = index_dir / "plugins" / "mod" / "mod_example.yml"
    entry = yaml_mod.safe_load(entry_path.read_text())
    fixed = dict(entry["releases"][0])
    fixed.update({"version": "1.1.0", "tag": "v1.1.0",
                  "published": "2026-02-01T12:30:00Z"})
    entry["releases"].append(fixed)
    entry_path.write_text(yaml_mod.safe_dump(entry, sort_keys=False))

    out2 = tmp_path / "site-fixed"
    site_generate(index_dir, "https://repo.test", out2)
    html2 = (out2 / "plugin" / "mod_example.html").read_text()
    assert "is not affected by any published advisory" in html2
    assert 'class="adv open sev-high"' not in html2     # no active card
    assert "Past advisories:" in html2
    assert "/advisories/CAMP-2026-0001.html" in html2   # permalink survives
    assert 'class="vrow rel-row vrow-adv"' in html2     # old row marked
    assert '<span class="advtag">· advisory</span>' in html2


def test_site_generates_advisory_permalinks(index_dir, tmp_path):
    _write_advisory(index_dir)
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)

    page = (out / "advisories" / "CAMP-2026-0001.html").read_text()
    assert "SQL injection in report page" in page
    assert "mod_example" in page
    assert "Update immediately." in page

    listing = (out / "advisories" / "index.html").read_text()
    assert "CAMP-2026-0001" in listing


def test_composer_advisory_link_resolves_to_permalink(index_dir):
    from camp.composer import generate_advisories
    _write_advisory(index_dir)
    advisories = generate_advisories(index_dir, "https://repo.test")
    published = advisories["advisories"]["tester/moodle-mod_example"]
    assert published[0]["link"] == "https://repo.test/advisories/CAMP-2026-0001.html"


def test_next_id_is_sequential(index_dir):
    assert next_id(index_dir, 2026) == "CAMP-2026-0001"
    _write_advisory(index_dir, advisory_id="CAMP-2026-0001")
    _write_advisory(index_dir, advisory_id="CAMP-2026-0007")
    assert next_id(index_dir, 2026) == "CAMP-2026-0008"
    assert next_id(index_dir, 2027) == "CAMP-2027-0001"
