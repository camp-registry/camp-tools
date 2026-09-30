"""validate: did-you-mean on unknown keys and the claim checklist hint
(camp-tools#12). No network."""

import yaml

from camp.validate import validate_entry

BASE = {
    "component": "local_x",
    "source": "https://github.com/holder/moodle-local_x",
    "maintainers": [{"github": "holder"}],
    "tier": 0,
    "releases": [],
}


def _entry(tmp_path, **over):
    d = dict(BASE, **over)
    path = tmp_path / "plugins" / "local" / "local_x.yml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(d, sort_keys=False))
    return path


def test_unknown_key_gets_a_suggestion(tmp_path):
    problems = validate_entry(_entry(tmp_path, sumary="typo"))
    assert any("'sumary' was unexpected" in p and "did you mean 'summary'" in p
               for p in problems)
    # a Tier 0 typo is not a claim: no checklist
    assert not any(p.startswith("hint:") for p in problems)


def test_claim_with_misspelt_and_missing_keys_gets_the_checklist(tmp_path):
    problems = validate_entry(_entry(tmp_path, tier=1, security="https://x.example/sec"))
    joined = "\n".join(problems)
    assert "did you mean 'security-contact'" in joined
    assert "'labels' is a required property" in joined
    hints = [p for p in problems if p.startswith("hint:")]
    assert len(hints) == 1
    assert "security-contact, labels, and tier: 1" in hints[0]
    assert "AUTHORS.md" in hints[0]
    assert problems[-1] == hints[0]  # the hint closes the list


def test_claim_missing_only_labels_still_gets_the_checklist(tmp_path):
    problems = validate_entry(_entry(
        tmp_path, tier=1, **{"security-contact": "https://x.example/sec"}))
    assert any("'labels' is a required property" in p for p in problems)
    assert any(p.startswith("hint:") for p in problems)


def test_complete_claim_is_clean(tmp_path):
    problems = validate_entry(_entry(
        tmp_path, tier=1, labels=["fully-free"],
        **{"security-contact": "https://github.com/holder/moodle-local_x/security"}))
    assert problems == []


def test_unrelated_key_has_no_suggestion(tmp_path):
    problems = validate_entry(_entry(tmp_path, zzzz="x"))
    assert any("'zzzz' was unexpected" in p and "did you mean" not in p
               for p in problems)
