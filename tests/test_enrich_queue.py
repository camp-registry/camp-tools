"""The rolling metrics refresh must pick the stalest entries, not the first
stale ones in path order, and must not let dead-weight entries take a slot
from every run (camp-index#317)."""
import datetime
import json

import yaml

import camp.scan as scan_mod


def _entry(index, plugintype, name, *, checked=None, tier=0, summary="x",
           source=None):
    d = index / "plugins" / plugintype
    d.mkdir(parents=True, exist_ok=True)
    entry = {"component": name, "source": source or f"https://github.com/u/{name}",
             "maintainers": [{"github": "u"}], "tier": tier, "status": "active",
             "releases": [], "license": "GPL-3.0"}
    if summary:
        entry["summary"] = summary
    if checked is not None:
        entry["metrics"] = {"updated": "2026-01-01T00:00:00Z", "stars": 1,
                            "forks": 0, "open-issues": 0, "archived": False,
                            "checked": checked}
    path = d / f"{name}.yml"
    path.write_text(yaml.safe_dump(entry, sort_keys=False))
    return path


def _days_ago(n):
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


def _fake_request(calls, gone=()):
    def fake(url, token, log=print, **kwargs):
        calls.append(url)
        name = url.rsplit("/", 1)[1]
        if "releases/latest" in url:
            return 404, b"{}", {}
        if name in gone:
            return 404, b"{}", {}
        return 200, json.dumps({
            "full_name": f"u/{name}", "pushed_at": "2026-07-01T00:00:00Z",
            "stargazers_count": 7, "forks_count": 0, "open_issues_count": 0,
            "archived": True}).encode(), {}
    return fake


def test_enrich_refreshes_stalest_first_regardless_of_path_order(tmp_path, monkeypatch):
    index = tmp_path / "index"
    _entry(index, "auth", "auth_early", checked=_days_ago(20))
    _entry(index, "mod", "mod_never")                   # no metrics at all
    late = _entry(index, "theme", "theme_late", checked=_days_ago(60))
    calls = []
    monkeypatch.setattr(scan_mod, "_request", _fake_request(calls))
    monkeypatch.setattr(scan_mod, "_fetch_version_php_text", lambda *a, **k: None)

    stats = scan_mod.enrich(index, token="x", readme=False, stale_days=14,
                            limit=2, log=lambda *a: None)

    assert stats["metrics"] == 2
    repos = [u.rsplit("/", 1)[1] for u in calls if "releases" not in u]
    assert repos == ["mod_never", "theme_late"]        # never-checked first, then oldest
    doc = yaml.safe_load(late.read_text())
    assert doc["metrics"]["archived"] is True
    assert doc["metrics"]["checked"] == datetime.date.today().isoformat()


def test_enrich_gone_and_unsupported_repos_rotate_out_of_the_queue(tmp_path, monkeypatch):
    index = tmp_path / "index"
    gone = _entry(index, "auth", "auth_gone", checked=_days_ago(90))
    bb = _entry(index, "block", "block_bb", checked=_days_ago(80),
                source="https://bitbucket.org/u/block_bb")
    calls = []
    monkeypatch.setattr(scan_mod, "_request", _fake_request(calls, gone={"auth_gone"}))

    stats = scan_mod.enrich(index, token="x", readme=False, stale_days=14,
                            log=lambda *a: None)

    assert stats["gone"] == 1 and stats["unsupported"] == 1 and stats["metrics"] == 0
    for path in (gone, bb):
        doc = yaml.safe_load(path.read_text())
        assert doc["metrics"]["checked"] == datetime.date.today().isoformat()
        assert doc["metrics"]["stars"] == 1              # old data kept, only the stamp moves

    # Second run the same day: both are fresh now and cost no request.
    calls.clear()
    stats = scan_mod.enrich(index, token="x", readme=False, stale_days=14,
                            log=lambda *a: None)
    assert calls == [] and stats["skipped"] == 2


def test_enrich_summary_attempt_rides_the_metrics_cycle(tmp_path, monkeypatch):
    """A tier 0 entry with no summary used to be contacted every day for its
    README, whatever its metrics age, and each such fetch took a slot."""
    index = tmp_path / "index"
    _entry(index, "mod", "mod_nosummary", checked=_days_ago(2), summary="")
    calls = []
    monkeypatch.setattr(scan_mod, "_request", _fake_request(calls))
    monkeypatch.setattr(scan_mod, "_fetch_readme_summary",
                        lambda *a, **k: calls.append("readme") or None)

    stats = scan_mod.enrich(index, token="x", readme=True, stale_days=14,
                            log=lambda *a: None)
    assert calls == [] and stats["skipped"] == 1

    # Once its metrics are due, the summary is attempted in the same pass.
    _entry(index, "mod", "mod_nosummary", checked=_days_ago(30), summary="")
    monkeypatch.setattr(scan_mod, "_fetch_version_php_text", lambda *a, **k: None)
    stats = scan_mod.enrich(index, token="x", readme=True, stale_days=14,
                            log=lambda *a: None)
    assert stats["metrics"] == 1 and "readme" in calls
