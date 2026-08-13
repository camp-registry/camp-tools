"""utilities/ listing validation (camp-docs#4): schema + the
monitorability fence + claim invariants."""

import yaml

from camp.validate import validate_utility


def _write(tmp_path, name="moosh", filename=None, **overrides):
    entry = {
        "name": name,
        "display-name": "Moosh",
        "summary": "MOOdle SHell.",
        "category": "cli",
        "source": "https://github.com/tmuras/moosh",
        "source-repo-id": 6603614,
        "install": ["composer"],
        "license": "GPL-3.0",
        "first-seen": "2026-08-12",
    }
    entry.update(overrides)
    entry = {k: v for k, v in entry.items() if v is not None}
    d = tmp_path / "utilities"
    d.mkdir(exist_ok=True)
    path = d / (filename or f"{name}.yml")
    path.write_text(yaml.safe_dump(entry))
    return path


def test_valid_utility_passes(tmp_path):
    assert validate_utility(_write(tmp_path)) == []


def test_filename_must_match_slug(tmp_path):
    problems = validate_utility(_write(tmp_path, filename="wrong.yml"))
    assert any("belongs at utilities/moosh.yml" in p for p in problems)


def test_component_style_fields_rejected(tmp_path):
    problems = validate_utility(_write(tmp_path, component="local_moosh"))
    assert any("component" in p for p in problems)


def test_unmonitored_host_needs_release_channel(tmp_path):
    problems = validate_utility(_write(
        tmp_path, source="https://bitbucket.org/dw8/tool"))
    assert any("machine-monitorable" in p for p in problems)
    # a declared, implemented channel satisfies the fence
    assert validate_utility(_write(
        tmp_path, source="https://bitbucket.org/dw8/tool",
        **{"release-channel": "openvsx:ns/ext"})) == []


def test_unknown_channel_scheme_fails_fence(tmp_path):
    problems = validate_utility(_write(
        tmp_path, **{"release-channel": "chrome-store:foo/bar"}))
    assert any("not implemented by camp-tools" in p for p in problems)


def test_claimed_requires_maintainers(tmp_path):
    problems = validate_utility(_write(tmp_path, claimed="2026-08-13"))
    assert problems == ["claimed entries must list maintainers"]
    assert validate_utility(_write(
        tmp_path, claimed="2026-08-13",
        maintainers=[{"github": "tmuras"}])) == []


def test_category_vocabulary_enforced(tmp_path):
    problems = validate_utility(_write(tmp_path, category="webapp"))
    assert any("category" in p for p in problems)
