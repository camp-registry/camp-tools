"""Organization-level claim sweep."""

import yaml
import pytest

from camp.orgclaim import ManifestError, org_claim, parse_manifest

MANIFEST = """\
maintainers:
- github: brendanheywood
- github: keevan
security-contact: https://github.com/catalyst/moodle-tool_objectfs/security
labels:
- fully-free
overrides:
  local_paid:
    labels: [external-account, paid-service]
exclude:
- local_unwanted
"""


def _write_entry(index, component, **kw):
    plugin_type = component.split("_")[0]
    entry = {"component": component,
             "source": kw.pop("source",
                              f"https://github.com/catalyst/moodle-{component}"),
             "maintainers": [{"github": "catalyst"}],
             "tier": kw.pop("tier", 0), "status": kw.pop("status", "active"),
             "discovered": "2026-07-11", "releases": [], "license": "GPL-3.0"}
    entry.update(kw)
    d = index / "plugins" / plugin_type
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{component}.yml"
    with open(path, "w") as f:
        yaml.safe_dump(entry, f, sort_keys=False)
    return path


def _fetch(org, repo):
    return MANIFEST.encode()


def test_claims_unclaimed_org_entries(tmp_path):
    path = _write_entry(tmp_path, "tool_objectfs")
    report = org_claim(tmp_path, "catalyst", fetch=_fetch)
    assert report.claimed == ["tool_objectfs"]
    entry = yaml.safe_load(path.read_text())
    assert entry["tier"] == 1
    assert entry["maintainers"] == [{"github": "brendanheywood"},
                                    {"github": "keevan"}]
    assert entry["security-contact"].endswith("/security")
    assert entry["labels"] == ["fully-free"]


def test_claim_matches_hand_claim_key_order(tmp_path):
    path = _write_entry(tmp_path, "tool_objectfs")
    org_claim(tmp_path, "catalyst", fetch=_fetch)
    keys = list(yaml.safe_load(path.read_text()))
    assert keys[:6] == ["component", "source", "security-contact",
                        "maintainers", "tier", "labels"]


def test_never_touches_claimed_entries(tmp_path):
    path = _write_entry(tmp_path, "tool_claimed", tier=1,
                        labels=["fully-free"], **{
                            "security-contact": "owner@example.org"})
    before = path.read_text()
    report = org_claim(tmp_path, "catalyst", fetch=_fetch)
    assert report.conflicts == ["tool_claimed"]
    assert report.claimed == []
    assert path.read_text() == before


def test_other_org_and_platform_untouched(tmp_path):
    _write_entry(tmp_path, "mod_other",
                 source="https://github.com/otherorg/moodle-mod_other")
    _write_entry(tmp_path, "mod_lab",
                 source="https://gitlab.com/catalyst/moodle-mod_lab")
    # an org whose login merely starts with the target org's name
    _write_entry(tmp_path, "mod_near",
                 source="https://github.com/catalyst-labs/moodle-mod_near")
    report = org_claim(tmp_path, "catalyst", fetch=_fetch)
    assert report.claimed == []


def test_overrides_exclude_and_status(tmp_path):
    _write_entry(tmp_path, "local_paid")
    _write_entry(tmp_path, "local_unwanted")
    _write_entry(tmp_path, "local_moved", status="moved",
                 **{"moved-to": "mod_new"})
    report = org_claim(tmp_path, "catalyst", fetch=_fetch)
    assert report.claimed == ["local_paid"]
    assert report.excluded == ["local_unwanted"]
    assert report.skipped == ["local_moved"]
    entry = yaml.safe_load(
        (tmp_path / "plugins/local/local_paid.yml").read_text())
    assert entry["labels"] == ["external-account", "paid-service"]


def test_dry_run_writes_nothing(tmp_path):
    path = _write_entry(tmp_path, "tool_objectfs")
    before = path.read_text()
    report = org_claim(tmp_path, "catalyst", fetch=_fetch, dry_run=True)
    assert report.claimed == ["tool_objectfs"]
    assert path.read_text() == before


@pytest.mark.parametrize("mutation, message", [
    ("maintainers: []\n", "maintainers"),
    ("maintainers:\n- github: ''\n", "maintainers"),
    ("security-contact: ''\n", "security-contact"),
    ("labels: [made-up-label]\n", "labels"),
    ("overrides:\n  x:\n    labels: [nope]\n", "labels"),
])
def test_manifest_validation(mutation, message):
    base = yaml.safe_load(MANIFEST)
    base.update(yaml.safe_load(mutation))
    with pytest.raises(ManifestError, match=message):
        parse_manifest(yaml.safe_dump(base).encode())


def test_manifest_not_yaml():
    with pytest.raises(ManifestError, match="YAML"):
        parse_manifest(b"{ not: valid: yaml")


def test_claim_stamps_org(tmp_path):
    path = _write_entry(tmp_path, "tool_objectfs")
    org_claim(tmp_path, "catalyst", fetch=_fetch)
    entry = yaml.safe_load(path.read_text())
    assert entry["org-claim"] == "catalyst"


def test_resweep_updates_stamped_entries_only_manifest_fields(tmp_path):
    path = _write_entry(tmp_path, "tool_objectfs")
    org_claim(tmp_path, "catalyst", fetch=_fetch)
    # simulate a later Tier 2 entry with releases, then a manifest change
    entry = yaml.safe_load(path.read_text())
    entry["tier"] = 2
    entry["releases"] = [{"version": "1.0.0"}]
    with open(path, "w") as f:
        yaml.safe_dump(entry, f, sort_keys=False)
    changed = MANIFEST.replace("- github: keevan", "- github: newperson")
    report = org_claim(tmp_path, "catalyst",
                       fetch=lambda o, m: changed.encode())
    assert report.updated == ["tool_objectfs"]
    assert report.claimed == [] and report.conflicts == []
    entry = yaml.safe_load(path.read_text())
    assert {"github": "newperson"} in entry["maintainers"]
    assert entry["tier"] == 2
    assert entry["releases"] == [{"version": "1.0.0"}]


def test_resweep_unchanged_manifest_writes_nothing(tmp_path):
    path = _write_entry(tmp_path, "tool_objectfs")
    org_claim(tmp_path, "catalyst", fetch=_fetch)
    before = path.read_text()
    report = org_claim(tmp_path, "catalyst", fetch=_fetch)
    assert report.claimed == [] and report.updated == []
    assert path.read_text() == before


def test_individual_claim_still_conflicts_on_resweep(tmp_path):
    _write_entry(tmp_path, "tool_hand", tier=1, labels=["fully-free"],
                 **{"security-contact": "owner@example.org"})
    report = org_claim(tmp_path, "catalyst", fetch=_fetch)
    assert report.conflicts == ["tool_hand"]


def test_excluding_a_stamped_entry_reports_never_unclaims(tmp_path):
    path = _write_entry(tmp_path, "tool_objectfs")
    org_claim(tmp_path, "catalyst", fetch=_fetch)
    changed = MANIFEST.replace("exclude:\n- local_unwanted",
                               "exclude:\n- tool_objectfs")
    report = org_claim(tmp_path, "catalyst",
                       fetch=lambda o, m: changed.encode())
    assert report.excluded_claimed == ["tool_objectfs"]
    entry = yaml.safe_load(path.read_text())
    assert entry["tier"] == 1 and entry["org-claim"] == "catalyst"


def test_load_enrolled(tmp_path):
    from camp.orgclaim import load_enrolled
    assert load_enrolled(tmp_path) == []
    d = tmp_path / "discovery"
    d.mkdir()
    (d / "org-claims.yml").write_text(
        "orgs:\n  catalyst:\n    enrolled: '2026-08-17'\n    issue: 230\n")
    assert load_enrolled(tmp_path) == ["catalyst"]
