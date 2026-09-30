"""Component-name collision classification and the check-collisions
reporter/backfill (no network; the API is monkeypatched)."""

import json

import yaml

import camp.scan as scan
import pytest

from camp.scan import (Candidate, check_collisions, classify_existing,
                       load_ledger, record_outcome, resolve_collision,
                       save_ledger)

HOLDER = "holder/moodle-local_x"
RIVAL = "rival/moodle-local_x"
ROOT_SHA = "a" * 40


def _write_listing(index, component="local_x", source=f"https://github.com/{HOLDER}"):
    path = index / "plugins" / component.partition("_")[0] / f"{component}.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"component": component, "source": source}))
    return path


def _api(responses):
    """A fake scan._request: URL substring -> (status, obj, headers)."""
    calls = []

    def fake_request(url, token, retries=3):
        calls.append(url)
        for fragment, (status, obj, headers) in responses.items():
            if fragment in url:
                body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
                return status, body, headers
        raise AssertionError(f"unexpected API call: {url}")

    fake_request.calls = calls
    return fake_request


def test_same_repo_is_exists_without_network(tmp_path, monkeypatch):
    _write_listing(tmp_path)
    monkeypatch.setattr(scan, "_request", _api({}))  # any call would raise
    # Scheme, case, trailing slash, and .git must not defeat the comparison.
    outcome, detail = classify_existing(
        tmp_path, f"https://github.com/{HOLDER}.git/", "local_x", None)
    assert outcome == "exists"
    assert "first-come" in detail


def test_shared_history_is_copy(tmp_path, monkeypatch):
    _write_listing(tmp_path)
    last_url = f"https://api.github.com/repos/{RIVAL}/commits?per_page=1&page=57"
    monkeypatch.setattr(scan, "_request", _api({
        f"{RIVAL}/commits?per_page=1&page=57": (200, [{"sha": ROOT_SHA}], {}),
        f"{RIVAL}/commits?per_page=1":
            (200, [{"sha": "f" * 40}], {"Link": f'<{last_url}>; rel="last"'}),
        f"{HOLDER}/commits/{ROOT_SHA}": (200, {"sha": ROOT_SHA}, {}),
    }))
    outcome, detail = classify_existing(
        tmp_path, f"https://github.com/{RIVAL}", "local_x", None)
    assert outcome == "copy"
    assert HOLDER in detail and "local_x" in detail


def test_disjoint_history_is_name_collision(tmp_path, monkeypatch):
    _write_listing(tmp_path)
    monkeypatch.setattr(scan, "_request", _api({
        # Single-page history: no Link header, first response is the root.
        f"{RIVAL}/commits?per_page=1": (200, [{"sha": ROOT_SHA}], {}),
        f"{HOLDER}/commits/{ROOT_SHA}": (404, {}, {}),
    }))
    outcome, detail = classify_existing(
        tmp_path, f"https://github.com/{RIVAL}", "local_x", None)
    assert outcome == "name-collision"
    assert "NAMESPACE.md" in detail
    assert "inconclusive" not in detail


def test_probe_failure_is_collision_marked_inconclusive(tmp_path, monkeypatch):
    _write_listing(tmp_path)
    monkeypatch.setattr(scan, "_request", _api({
        f"{RIVAL}/commits?per_page=1": (500, {}, {}),
    }))
    outcome, detail = classify_existing(
        tmp_path, f"https://github.com/{RIVAL}", "local_x", None)
    assert outcome == "name-collision"
    assert "inconclusive" in detail


def test_record_outcome_carries_component_only_for_collision_classes():
    candidate = Candidate(
        full_name=RIVAL, html_url=f"https://github.com/{RIVAL}", owner="rival",
        description="", license_spdx="GPL-3.0", stars=0,
        default_branch="main", archived=False)
    ledger = {}
    record_outcome(ledger, candidate, "name-collision", "d", "2026-07-22",
                   component="local_x")
    assert ledger[RIVAL]["component"] == "local_x"
    record_outcome(ledger, candidate, "exists", "d", "2026-07-22",
                   component="local_x")
    assert "component" not in ledger[RIVAL]


def test_reclassify_splits_legacy_exists(tmp_path, monkeypatch):
    _write_listing(tmp_path)
    _write_listing(tmp_path, component="local_y",
                   source="https://github.com/holder/moodle-local_y")
    ledger = {
        # The listed repository itself, re-seen: must stay untouched.
        HOLDER: {"outcome": "exists",
                 "detail": "component local_x already registered (first-come, RFC §8)",
                 "first-seen": "2026-07-11", "last-checked": "2026-07-11"},
        # A different repository: must be probed and reclassified.
        RIVAL: {"outcome": "exists",
                "detail": "component local_x already registered (first-come, RFC §8)",
                "first-seen": "2026-07-11", "last-checked": "2026-07-11"},
        # Probe fails on both hosts: left as exists for a later run.
        "ghost/moodle-local_y": {
            "outcome": "exists",
            "detail": "component local_y already registered (first-come, RFC §8)",
            "first-seen": "2026-07-11", "last-checked": "2026-07-11"},
    }
    save_ledger(tmp_path, ledger)

    def fake_shares_history(candidate_url, listed_url, token):
        if RIVAL in candidate_url:
            return False
        return None  # ghost: inconclusive on github and gitlab alike

    monkeypatch.setattr(scan, "_shares_history", fake_shares_history)
    stats = check_collisions(tmp_path, reclassify=True, log=lambda *a: None)

    reloaded = load_ledger(tmp_path)
    assert reloaded[HOLDER]["outcome"] == "exists"
    assert reloaded[HOLDER]["last-checked"] == "2026-07-11"
    assert reloaded[RIVAL]["outcome"] == "name-collision"
    assert reloaded[RIVAL]["component"] == "local_x"
    assert reloaded[RIVAL]["first-seen"] == "2026-07-11"
    assert reloaded["ghost/moodle-local_y"]["outcome"] == "exists"
    assert stats["reclassified"] == 1
    assert stats["inconclusive"] == 1
    assert [name for name, _ in stats["collisions"]] == [RIVAL]


def test_reclassify_dry_run_writes_nothing(tmp_path, monkeypatch):
    _write_listing(tmp_path)
    ledger = {RIVAL: {"outcome": "exists",
                      "detail": "component local_x already registered (first-come, RFC §8)",
                      "first-seen": "2026-07-11", "last-checked": "2026-07-11"}}
    save_ledger(tmp_path, ledger)
    monkeypatch.setattr(scan, "_shares_history", lambda *a: False)
    stats = check_collisions(tmp_path, reclassify=True, dry_run=True,
                             log=lambda *a: None)
    assert stats["reclassified"] == 1
    assert load_ledger(tmp_path)[RIVAL]["outcome"] == "exists"


def test_component_filter(tmp_path):
    ledger = {
        RIVAL: {"outcome": "name-collision", "detail": "d",
                "component": "local_x",
                "first-seen": "2026-07-11", "last-checked": "2026-07-22"},
        "other/moodle-mod_z": {"outcome": "name-collision", "detail": "d",
                               "component": "mod_z",
                               "first-seen": "2026-07-11",
                               "last-checked": "2026-07-22"},
        "copycat/moodle-local_x": {"outcome": "copy", "detail": "d",
                                   "component": "local_x",
                                   "first-seen": "2026-07-11",
                                   "last-checked": "2026-07-22"},
    }
    save_ledger(tmp_path, ledger)
    stats = check_collisions(tmp_path, component="local_x",
                             include_copies=True, log=lambda *a: None)
    assert [name for name, _ in stats["collisions"]] == [RIVAL]
    assert [name for name, _ in stats["copies"]] == ["copycat/moodle-local_x"]


def test_gitlab_scan_probe_uses_github_token_not_gitlab(monkeypatch):
    # The classifier must be handed a GitHub token (or None), never the
    # GitLab token scan_gitlab runs with; a Bearer'd GitLab token would 401
    # every github.com probe and turn every copy into "inconclusive".
    import inspect
    src = inspect.getsource(scan.scan_gitlab)
    # the GitHub side comes from the self-refreshing App source (or the
    # GITHUB_TOKEN fallback inside it), never from the GitLab `token`
    assert "github_token = apptoken.token_from_env(" in src
    assert "candidate.html_url, component,\n                    token)" not in src


def test_reclassify_matches_same_run_duplicate_wording(tmp_path, monkeypatch):
    """The seeding scan recorded same-run duplicates as 'already indexed
    this run'; the first backfill's regex only matched 'already
    registered' and left 727 entries unclassified (camp-tools#11)."""
    _write_listing(tmp_path)
    ledger = {RIVAL: {
        "outcome": "exists",
        "detail": "component local_x already indexed this run",
        "first-seen": "2026-07-11", "last-checked": "2026-07-11"}}
    save_ledger(tmp_path, ledger)
    monkeypatch.setattr(scan, "_shares_history", lambda *a: False)
    stats = check_collisions(tmp_path, reclassify=True, log=lambda *a: None)
    assert stats["reclassified"] == 1
    assert load_ledger(tmp_path)[RIVAL]["outcome"] == "name-collision"


# --- recorded collision verdicts (camp-tools#57) ---------------------------

_COLLISION = {
    "outcome": "name-collision",
    "detail": "independent repository declaring local_x, held by "
              f"https://github.com/{HOLDER}; see NAMESPACE.md",
    "first-seen": "2026-07-11", "last-checked": "2026-09-05",
    "component": "local_x",
}
REF = "https://github.com/camp-registry/camp-index/pull/401"


def test_resolve_collision_writes_the_verdict(tmp_path):
    save_ledger(tmp_path, {RIVAL: dict(_COLLISION)})
    lines = []
    entry = resolve_collision(tmp_path, RIVAL.upper(), "copy-of-listed", REF,
                              decided="2026-09-30", log=lines.append)
    assert entry["resolution"] == {"verdict": "copy-of-listed", "ref": REF,
                                   "decided": "2026-09-30"}
    # persisted under the ledger's own key, case-insensitive lookup
    assert load_ledger(tmp_path)[RIVAL]["resolution"]["verdict"] == "copy-of-listed"
    assert lines and lines[0].startswith(f"resolved: {RIVAL}")


def test_resolve_collision_refusals(tmp_path):
    save_ledger(tmp_path, {
        RIVAL: dict(_COLLISION),
        "other/moodle-local_x": {"outcome": "copy", "detail": "d",
                                 "first-seen": "2026-07-11", "last-checked": "2026-07-11",
                                 "component": "local_x"},
    })
    with pytest.raises(KeyError):
        resolve_collision(tmp_path, "nobody/moodle-local_x", "copy-of-listed", REF)
    with pytest.raises(ValueError, match="not name-collision"):
        resolve_collision(tmp_path, "other/moodle-local_x", "copy-of-listed", REF)
    with pytest.raises(ValueError, match="verdict must be"):
        resolve_collision(tmp_path, RIVAL, "whatever", REF)
    with pytest.raises(ValueError, match="public issue or pull request URL"):
        resolve_collision(tmp_path, RIVAL, "copy-of-listed", "see chat")
    resolve_collision(tmp_path, RIVAL, "copy-of-listed", REF, log=lambda *a: None)
    with pytest.raises(ValueError, match="already resolved"):
        resolve_collision(tmp_path, RIVAL, "dispute-lost", REF)
    resolve_collision(tmp_path, RIVAL, "dispute-lost", REF, force=True,
                      log=lambda *a: None)
    assert load_ledger(tmp_path)[RIVAL]["resolution"]["verdict"] == "dispute-lost"


def test_check_collisions_hides_resolved_unless_asked(tmp_path):
    _write_listing(tmp_path)
    resolved = dict(_COLLISION, resolution={"verdict": "copy-of-listed",
                                            "ref": REF, "decided": "2026-09-30"})
    save_ledger(tmp_path, {RIVAL: resolved,
                           "third/moodle-local_x": dict(_COLLISION)})
    lines = []
    stats = check_collisions(tmp_path, component="local_x", log=lines.append)
    # the unresolved one still routes; the resolved one is out of the grep
    assert [n for n, _ in stats["collisions"]] == ["third/moodle-local_x"]
    assert [n for n, _ in stats["resolved"]] == [RIVAL]
    assert sum(1 for l in lines if l.startswith("name-collision:")) == 1
    assert not any(l.startswith("resolved:") for l in lines)
    assert "1 resolved (hidden" in lines[-1]

    lines = []
    check_collisions(tmp_path, component="local_x", include_resolved=True,
                     log=lines.append)
    assert any(l.startswith(f"resolved: {RIVAL}") and "copy-of-listed" in l
               for l in lines)
    assert sum(1 for l in lines if l.startswith("name-collision:")) == 1


def test_recheck_preserves_the_verdict():
    candidate = Candidate(
        full_name=RIVAL, html_url=f"https://github.com/{RIVAL}", owner="rival",
        description="", license_spdx="GPL-3.0", stars=0,
        default_branch="main", archived=False)
    ledger = {RIVAL: dict(_COLLISION, resolution={"verdict": "copy-of-listed",
                                                  "ref": REF, "decided": "2026-09-30"})}
    record_outcome(ledger, candidate, "name-collision", "re-derived detail",
                   "2026-10-05", component="local_x")
    assert ledger[RIVAL]["last-checked"] == "2026-10-05"
    assert ledger[RIVAL]["detail"] == "re-derived detail"
    assert ledger[RIVAL]["resolution"]["verdict"] == "copy-of-listed"
    # a record without a verdict gains none
    record_outcome(ledger, Candidate(
        full_name="third/moodle-local_x", html_url="", owner="third", description="",
        license_spdx="GPL-3.0", stars=0, default_branch="main", archived=False),
        "name-collision", "d", "2026-10-05", component="local_x")
    assert "resolution" not in ledger["third/moodle-local_x"]

