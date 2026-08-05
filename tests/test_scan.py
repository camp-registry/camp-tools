"""Scanner parsing and acceptance logic (no network)."""

import yaml

from camp.scan import COMPONENT_RE, Candidate, _entry_for, _is_gpl


def _candidate(**overrides):
    defaults = dict(full_name="o/moodle-mod_x", html_url="https://github.com/o/moodle-mod_x",
                    owner="o", description="A plugin", license_spdx="GPL-3.0",
                    stars=1, default_branch="main", archived=False)
    return Candidate(**{**defaults, **overrides})


def test_component_regex():
    assert COMPONENT_RE.search("$plugin->component = 'mod_forum';").group(1) == "mod_forum"
    assert COMPONENT_RE.search('$plugin->component = "qtype_stack" ;').group(1) == "qtype_stack"
    # legacy activity style
    assert COMPONENT_RE.search("$module->component = 'mod_old';").group(1) == "mod_old"
    # not frankenstyle
    assert COMPONENT_RE.search("$plugin->component = 'Forum';") is None


def test_gpl_family():
    assert _is_gpl("GPL-3.0")
    assert _is_gpl("GPL-2.0-or-later")
    assert _is_gpl("AGPL-3.0")
    assert not _is_gpl("MIT")
    assert not _is_gpl(None)
    assert not _is_gpl("NOASSERTION")


def test_entry_shape():
    entry = _entry_for(_candidate(), "mod_x", "2026-07-10")
    assert entry["tier"] == 0
    assert entry["releases"] == []
    assert entry["discovered"] == "2026-07-10"
    assert entry["summary"] == "A plugin"
    assert "labels" not in entry and "security-contact" not in entry


def test_entry_omits_empty_summary():
    entry = _entry_for(_candidate(description=""), "mod_x", "2026-07-10")
    assert "summary" not in entry


def test_ledger_records_and_skips(tmp_path):
    from camp.scan import (load_ledger, record_outcome, save_ledger, should_skip)
    ledger = load_ledger(tmp_path)
    assert ledger == {}

    candidate = _candidate(license_spdx="MIT")
    record_outcome(ledger, candidate, "bad-license", "license: MIT", "2026-07-10")
    save_ledger(tmp_path, ledger)

    reloaded = load_ledger(tmp_path)
    record = reloaded["o/moodle-mod_x"]
    assert record["outcome"] == "bad-license"
    assert record["detail"] == "license: MIT"
    assert record["first-seen"] == "2026-07-10"

    # within the recheck window: skipped; after it: re-evaluated
    assert should_skip(reloaded, "o/moodle-mod_x", "2026-07-20", recheck_days=30)
    assert not should_skip(reloaded, "o/moodle-mod_x", "2026-09-01", recheck_days=30)
    # unknown repos and written entries are never skipped
    assert not should_skip(reloaded, "other/repo", "2026-07-20")
    record_outcome(reloaded, candidate, "written", "listed", "2026-07-21")
    assert not should_skip(reloaded, "o/moodle-mod_x", "2026-07-22")


def test_ledger_preserves_first_seen(tmp_path):
    from camp.scan import record_outcome
    ledger = {}
    candidate = _candidate()
    record_outcome(ledger, candidate, "no-version-php", "x", "2026-01-01")
    record_outcome(ledger, candidate, "bad-license", "y", "2026-06-01")
    record = ledger["o/moodle-mod_x"]
    assert record["first-seen"] == "2026-01-01"
    assert record["last-checked"] == "2026-06-01"
    assert record["outcome"] == "bad-license"


def test_compatible_licenses_accepted():
    from camp.scan import _is_acceptable_license
    assert _is_acceptable_license("GPL-3.0")
    assert _is_acceptable_license("MIT")
    assert _is_acceptable_license("Apache-2.0")
    assert _is_acceptable_license("BSD-3-Clause")
    assert not _is_acceptable_license("NOASSERTION")
    assert not _is_acceptable_license("CC-BY-SA-4.0")
    assert not _is_acceptable_license(None)


def test_entry_records_license():
    entry = _entry_for(_candidate(license_spdx="MIT"), "mod_x", "2026-07-11")
    assert entry["license"] == "MIT"
    entry = _entry_for(_candidate(license_spdx=None), "mod_x", "2026-07-11")
    assert "license" not in entry


def test_search_skips_private_repos():
    from camp.scan import _search
    import camp.scan as scan_mod
    import json
    payload = {"total_count": 2, "items": [
        {"full_name": "u/moodle-mod_pub", "html_url": "https://github.com/u/moodle-mod_pub",
         "owner": {"login": "u"}, "description": "", "private": False,
         "visibility": "public", "stargazers_count": 1,
         "default_branch": "main", "archived": False},
        {"full_name": "u/moodle-mod_secret", "html_url": "https://github.com/u/moodle-mod_secret",
         "owner": {"login": "u"}, "description": "client work", "private": True,
         "visibility": "private", "stargazers_count": 0,
         "default_branch": "main", "archived": False},
    ]}
    calls = {"n": 0}
    def fake_request(url, token, log=print, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return 200, json.dumps(payload).encode(), {}
        return 200, json.dumps({"total_count": 2, "items": []}).encode(), {}
    orig = scan_mod._request
    scan_mod._request = fake_request
    try:
        candidates, total = _search("q", 10, None, print)
    finally:
        scan_mod._request = orig
    names = [c.full_name for c in candidates]
    assert "u/moodle-mod_pub" in names
    assert "u/moodle-mod_secret" not in names


def test_site_shows_compatible_license_badge(index_dir, tmp_path):
    import yaml
    from camp.site import generate as site_generate
    entry_path = index_dir / "plugins" / "mod" / "mod_example.yml"
    entry = yaml.safe_load(entry_path.read_text())
    entry["license"] = "MIT"
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))

    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "MIT · GPL-compatible" in html

    # GPL-family stays unmarked
    entry["license"] = "GPL-3.0"
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "GPL-compatible" not in html


def test_classify_license_text():
    from camp.scan import classify_license_text
    gpl3 = "Preamble blah.\nGNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n..."
    assert classify_license_text(gpl3) == "GPL-3.0"
    gpl2 = "GNU GENERAL PUBLIC LICENSE\n   Version 2, June 1991"
    assert classify_license_text(gpl2) == "GPL-2.0"
    agpl = "custom header\nGNU AFFERO GENERAL PUBLIC LICENSE Version 3"
    assert classify_license_text(agpl) == "AGPL-3.0"
    mit = "MyPlugin License\n\nPermission is hereby granted, free of charge, to any person..."
    assert classify_license_text(mit) == "MIT"
    apache = "Apache License\nVersion 2.0, January 2004\nhttp://www.apache.org/licenses/"
    assert classify_license_text(apache) == "Apache-2.0"
    bsd3 = ("Redistribution and use in source and binary forms, with or without "
            "modification... Neither the name of the copyright holder...")
    assert classify_license_text(bsd3) == "BSD-3-Clause"
    bsd2 = "Redistribution and use in source and binary forms, with or without modification"
    assert classify_license_text(bsd2) == "BSD-2-Clause"
    assert classify_license_text("All rights reserved. Proprietary.") is None
    # whitespace/case robustness (reflowed text is the common NOASSERTION cause)
    assert classify_license_text("gnu   general\n\npublic  LICENSE  ...  version 3") == "GPL-3.0"


def test_name_matches_component():
    from camp.scan import _name_matches_component
    assert _name_matches_component("o/moodle-mod_googlemeet", "mod_googlemeet")
    assert _name_matches_component("trampgeek/moodle-qtype_coderunner", "qtype_coderunner")
    assert _name_matches_component("me/coderunner", "qtype_coderunner")  # short name alone
    assert _name_matches_component("x/moodle-theme_boost_union", "theme_boost_union")
    assert not _name_matches_component("onyetapp/WORDPRESS-02-onyetmpdf", "mod_ompdf")
    assert not _name_matches_component("someone/random-repo", "mod_quiz")


def test_gitlab_entry_uses_gitlab_maintainer():
    from camp.scan import _entry_for
    c = Candidate(full_name="grp/moodle-mod_x", html_url="https://gitlab.com/grp/moodle-mod_x",
                  owner="grp", description="", license_spdx="GPL-3.0", stars=2,
                  default_branch="main", archived=False, platform="gitlab")
    entry = _entry_for(c, "mod_x", "2026-07-11")
    assert entry["maintainers"] == [{"gitlab": "grp"}]
    assert entry["source"] == "https://gitlab.com/grp/moodle-mod_x"


def test_gitlab_license_map():
    from camp.scan import GITLAB_LICENSE_MAP
    assert GITLAB_LICENSE_MAP["gpl-3.0"] == "GPL-3.0"
    assert GITLAB_LICENSE_MAP["apache-2.0"] == "Apache-2.0"
    assert "cc-by-sa-4.0" not in GITLAB_LICENSE_MAP


def test_gitlab_maintainer_validates(index_dir, tmp_path):
    """A gitlab-only maintainer must satisfy the schema."""
    import yaml
    from camp.validate import validate_entry
    entry_path = index_dir / "plugins" / "mod" / "mod_gl.yml"
    entry = {
        "component": "mod_gl",
        "source": "https://gitlab.com/grp/moodle-mod_gl",
        "maintainers": [{"gitlab": "grp"}],
        "tier": 0, "status": "active", "discovered": "2026-07-11", "releases": [],
    }
    entry_path.write_text(yaml.safe_dump(entry))
    assert validate_entry(entry_path) == []


def test_scan_admits_license_from_version_php_header(tmp_path, monkeypatch):
    """A repo GitHub reports as license=None must still be admitted when its
    version.php carries the standard Moodle GPL header (the local_recompletion
    case: no LICENSE file, GPL grant in the header)."""
    import camp.scan as scan
    index = tmp_path / "index"
    (index / "plugins").mkdir(parents=True)

    candidate = _candidate(full_name="danmarsden/moodle-local_recompletion",
                           html_url="https://github.com/danmarsden/moodle-local_recompletion",
                           owner="danmarsden", license_spdx=None,
                           default_branch="MOODLE_405_STABLE")
    version_php = ("<?php\n// it under the terms of the GNU General Public License as\n"
                   "// published by the Free Software Foundation, either version 3.\n"
                   "$plugin->component = 'local_recompletion';\n")
    monkeypatch.setattr(scan, "_search", lambda *a, **k: ([candidate], 1))
    monkeypatch.setattr(scan, "_fetch_component",
                        lambda c, t, log=None: ("ok", "local_recompletion", version_php))

    results = scan.scan(index, queries=["x"], limit=1, token="fake")
    assert results[0].outcome == "written"
    written = yaml.safe_load((index / "plugins" / "local" / "local_recompletion.yml").read_text())
    assert written["license"] == "GPL-3.0"


def test_default_query_specs_include_frankenstyle_by_updated():
    from camp.scan import DEFAULT_QUERY_SPECS
    specs = dict(DEFAULT_QUERY_SPECS)
    # topic queries stay stars-sorted; name-prefix queries use recent activity
    assert specs["moodle in:name fork:false"] == "stars"
    assert specs["moodle-mod_ in:name fork:false"] == "updated"
    assert specs["moodle-local_ in:name fork:false"] == "updated"
    # every prefix query is a name search sorted by updated
    prefix_specs = [(q, s) for q, s in DEFAULT_QUERY_SPECS if q.startswith("moodle-")]
    assert prefix_specs and all(s == "updated" and "in:name" in q for q, s in prefix_specs)


def test_date_windows_partition_until_under_target(monkeypatch):
    """Bisection must keep splitting until every window is under the target,
    and cover the whole range. Simulate a corpus of 3000 evenly-spread repos."""
    import camp.scan as scan

    def fake_search(query, limit, token, log, sort="stars"):
        # parse the pushed:START..END window and return a count proportional
        # to the span (3000 repos spread evenly over the 2010..today range).
        import datetime as dt
        span = query.split("pushed:")[1]
        a, b = (dt.date.fromisoformat(x) for x in span.split(".."))
        total_days = (dt.date.today() + dt.timedelta(days=1)
                      - dt.date.fromisoformat(scan.GITHUB_EPOCH)).days
        count = round(3000 * (b - a).days / total_days)
        return [], count

    monkeypatch.setattr(scan, "_search", fake_search)
    windows = scan._date_windows("moodle-local_ in:name", token=None, log=lambda *_: None)
    assert len(windows) >= 4  # 3000 / 900 -> at least 4 windows
    # every window is under the target when re-counted
    for w in windows:
        _, n = fake_search(w, 1, None, lambda *_: None)
        assert n < scan.SHARD_TARGET
    # windows are contiguous and span the full range (ignoring shared boundaries)
    import datetime as dt
    spans = sorted((w.split("pushed:")[1].split("..") for w in windows))
    assert spans[0][0] == scan.GITHUB_EPOCH


def test_fetch_component_survives_timeout(monkeypatch):
    """A socket-read TimeoutError must not crash the sweep — it becomes a
    retried, then transient, result (the block_/mod_ crash regression)."""
    import camp.scan as scan
    calls = {"n": 0}

    def always_timeout(*a, **k):
        calls["n"] += 1
        raise TimeoutError("read timed out")

    monkeypatch.setattr(scan.time, "sleep", lambda *_: None)  # no real waiting
    monkeypatch.setattr(scan.urllib.request, "urlopen", always_timeout)
    status, component, text = scan._fetch_component(
        _candidate(default_branch="main"), token="fake")
    assert status == "transient" and component is None
    assert calls["n"] == 3  # retried, then gave up — did not raise


def test_fetch_component_encodes_non_ascii_branch(monkeypatch):
    """A non-ASCII default branch must be percent-encoded, not sent raw —
    urllib's putrequest crashes with UnicodeEncodeError otherwise (killed
    the first scheduled discovery scan)."""
    import io
    import urllib.parse

    import camp.scan as scan
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        request.full_url.encode("ascii")  # what putrequest requires
        response = io.BytesIO(b"$plugin->component = 'mod_x';")
        response.status = 200
        return response

    monkeypatch.setattr(scan.urllib.request, "urlopen", fake_urlopen)
    for token in ("fake", None):  # contents API and raw-host paths
        status, component, _ = scan._fetch_component(
            _candidate(default_branch="принят-über"), token=token)
        assert status == "ok" and component == "mod_x"
        assert urllib.parse.quote("принят-über", safe="") in seen["url"]


def test_request_survives_timeout(monkeypatch):
    import camp.scan as scan
    monkeypatch.setattr(scan.time, "sleep", lambda *_: None)
    monkeypatch.setattr(scan.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    status, body, headers = scan._request("https://api.github.com/x", token=None)
    assert status == 0 and body == b""


def test_enrich_detects_renamed_repos(tmp_path, monkeypatch):
    """GitHub 301s renamed repos forever, so metrics keep flowing under the
    stale name — only comparing full_name catches a migration. Tier 0 gets
    auto-canonicalized; claimed entries are flagged, never rewritten."""
    import json as _json

    import yaml as _yaml

    import camp.scan as scan_mod

    index = tmp_path / "index"
    for name, tier in (("mod_zero", 0), ("mod_claimed", 2)):
        d = index / "plugins" / "mod"
        d.mkdir(parents=True, exist_ok=True)
        entry = {"component": name, "source": f"https://github.com/olduser/{name}",
                 "maintainers": [{"github": "olduser"}], "tier": tier,
                 "status": "active", "releases": [], "license": "GPL-3.0"}
        if tier >= 1:
            entry["labels"] = ["fully-free"]
            entry["security-contact"] = "https://example.org/sec"
            entry["releases"] = []
        (d / f"{name}.yml").write_text(_yaml.safe_dump(entry, sort_keys=False))

    def fake_request(url, token, log=print, **kwargs):
        # the API answers the old path with the repo's NEW identity
        name = url.rsplit("/", 1)[1]
        body = _json.dumps({
            "full_name": f"newuser/{name}", "pushed_at": "2026-07-01T00:00:00Z",
            "stargazers_count": 1, "forks_count": 0, "open_issues_count": 0,
            "archived": False}).encode()
        if "releases/latest" in url:
            return 404, b"{}", {}
        return 200, body, {}

    monkeypatch.setattr(scan_mod, "_request", fake_request)
    stats = scan_mod.enrich(index, token="x", readme=False, log=lambda *a: None)

    assert stats["renamed"] == 1 and stats["flagged-renames"] == 1
    zero = _yaml.safe_load((index / "plugins" / "mod" / "mod_zero.yml").read_text())
    assert zero["source"] == "https://github.com/newuser/mod_zero"
    assert "renamed-to" not in (zero.get("metrics") or {})
    claimed = _yaml.safe_load((index / "plugins" / "mod" / "mod_claimed.yml").read_text())
    assert claimed["source"] == "https://github.com/olduser/mod_claimed"
    assert claimed["metrics"]["renamed-to"] == "https://github.com/newuser/mod_claimed"


def test_enrich_stale_days_rolling_refresh(tmp_path, monkeypatch):
    import datetime as _dt
    import json as _json

    import yaml as _yaml

    import camp.scan as scan_mod

    index = tmp_path / "index"
    d = index / "plugins" / "mod"
    d.mkdir(parents=True)
    fresh = (_dt.date.today() - _dt.timedelta(days=2)).isoformat()
    stale = (_dt.date.today() - _dt.timedelta(days=40)).isoformat()
    for name, checked in (("mod_fresh", fresh), ("mod_stale", stale)):
        (d / f"{name}.yml").write_text(_yaml.safe_dump({
            "component": name, "source": f"https://github.com/u/{name}",
            "maintainers": [{"github": "u"}], "tier": 0, "status": "active",
            "releases": [], "license": "GPL-3.0",
            "metrics": {"updated": "2026-01-01T00:00:00Z", "stars": 0,
                        "forks": 0, "open-issues": 0, "archived": False,
                        "checked": checked}}, sort_keys=False))

    calls = []

    def fake_request(url, token, log=print, **kwargs):
        calls.append(url)
        if "releases/latest" in url:
            return 404, b"{}", {}
        name = url.rsplit("/", 1)[1]
        return 200, _json.dumps({
            "full_name": f"u/{name}", "pushed_at": "2026-07-01T00:00:00Z",
            "stargazers_count": 5, "forks_count": 0, "open_issues_count": 0,
            "archived": False}).encode(), {}

    monkeypatch.setattr(scan_mod, "_request", fake_request)
    stats = scan_mod.enrich(index, token="x", readme=False, stale_days=14,
                            log=lambda *a: None)
    assert stats["metrics"] == 1                       # only the stale one
    assert all("mod_stale" in u for u in calls)
    doc = _yaml.safe_load((d / "mod_stale.yml").read_text())
    assert doc["metrics"]["stars"] == 5


def _repoid_index(tmp_path, entries):
    import yaml as _yaml
    index = tmp_path / "index"
    d = index / "plugins" / "mod"
    d.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        (d / f"{entry['component']}.yml").write_text(
            _yaml.safe_dump(entry, sort_keys=False))
    return index


def _claimed(name, source, **extra):
    entry = {"component": name, "source": source,
             "maintainers": [{"github": "u"}], "tier": 1,
             "security-contact": "https://example.org/sec",
             "labels": ["fully-free"], "status": "active", "releases": []}
    entry.update(extra)
    return entry


def test_fill_repo_ids_backfill_sweep(tmp_path, monkeypatch):
    """No components = every Tier 1+ entry missing the field; tier 0 and
    already-anchored entries are left alone (and not re-fetched)."""
    import json as _json

    import yaml as _yaml

    import camp.scan as scan_mod

    index = _repoid_index(tmp_path, [
        _claimed("mod_missing", "https://github.com/u/moodle-mod_missing"),
        _claimed("mod_anchored", "https://github.com/u/moodle-mod_anchored",
                 **{"source-repo-id": 11}),
        {"component": "mod_zero", "source": "https://github.com/u/moodle-mod_zero",
         "maintainers": [{"github": "u"}], "tier": 0, "status": "active",
         "releases": []},
    ])
    calls = []

    def fake_request(url, token, log=print, **kwargs):
        calls.append(url)
        return 200, _json.dumps({"id": 4242}).encode(), {}

    monkeypatch.setattr(scan_mod, "_request", fake_request)
    failed = scan_mod.fill_repo_ids(index, log=lambda *a: None)

    assert failed == []
    assert len(calls) == 1 and "mod_missing" in calls[0]
    got = _yaml.safe_load(
        (index / "plugins" / "mod" / "mod_missing.yml").read_text())
    assert got["source-repo-id"] == 4242
    # the anchor reads directly after the path it anchors
    keys = list(got.keys())
    assert keys.index("source-repo-id") == keys.index("source") + 1
    anchored = _yaml.safe_load(
        (index / "plugins" / "mod" / "mod_anchored.yml").read_text())
    assert anchored["source-repo-id"] == 11
    zero = _yaml.safe_load(
        (index / "plugins" / "mod" / "mod_zero.yml").read_text())
    assert "source-repo-id" not in zero


def test_fill_repo_ids_named_component_overwrites(tmp_path, monkeypatch):
    """Naming a component re-resolves and overwrites: the repoint case."""
    import json as _json

    import yaml as _yaml

    import camp.scan as scan_mod

    index = _repoid_index(tmp_path, [
        _claimed("mod_repointed", "https://github.com/newowner/moodle-mod_repointed",
                 **{"source-repo-id": 11}),
    ])
    monkeypatch.setattr(scan_mod, "_request",
                        lambda url, token, log=print:
                        (200, _json.dumps({"id": 9000}).encode(), {}))
    failed = scan_mod.fill_repo_ids(index, ["mod_repointed"],
                                    log=lambda *a: None)
    assert failed == []
    got = _yaml.safe_load(
        (index / "plugins" / "mod" / "mod_repointed.yml").read_text())
    assert got["source-repo-id"] == 9000


def test_fill_repo_ids_refuses_tier0_and_reports_failures(tmp_path, monkeypatch):
    import json as _json

    import camp.scan as scan_mod

    index = _repoid_index(tmp_path, [
        {"component": "mod_zero", "source": "https://github.com/u/moodle-mod_zero",
         "maintainers": [{"github": "u"}], "tier": 0, "status": "active",
         "releases": []},
        _claimed("mod_gone", "https://github.com/u/moodle-mod_gone"),
    ])
    monkeypatch.setattr(scan_mod, "_request",
                        lambda url, token, log=print: (404, b"{}", {}))
    failed = scan_mod.fill_repo_ids(index, ["mod_zero", "mod_gone", "mod_absent"],
                                    log=lambda *a: None)
    assert sorted(failed) == ["mod_absent", "mod_gone", "mod_zero"]


def test_fetch_repo_id_gitlab_encodes_path(monkeypatch):
    import json as _json

    import camp.scan as scan_mod

    seen = {}

    def fake_request(url, token, log=print, **kwargs):
        seen["url"] = url
        return 200, _json.dumps({"id": 84895550}).encode(), {}

    monkeypatch.setattr(scan_mod, "_request", fake_request)
    status, repo_id = scan_mod._fetch_repo_id(
        "https://gitlab.com/grp/sub/moodle-local_x", None, log=lambda *a: None)
    assert status == "ok" and repo_id == 84895550
    assert seen["url"] == ("https://gitlab.com/api/v4/projects/"
                           "grp%2Fsub%2Fmoodle-local_x")


def test_refresh_metrics_keeps_repo_id_current(tmp_path, monkeypatch):
    """A repoint's refresh-metrics pass updates the OIDC anchor with the
    metrics, and an id fetch failure does not fail the refresh."""
    import json as _json

    import yaml as _yaml

    import camp.scan as scan_mod

    index = _repoid_index(tmp_path, [
        _claimed("mod_moved", "https://github.com/newowner/moodle-mod_moved",
                 **{"source-repo-id": 11}),
    ])

    def fake_request(url, token, log=print, **kwargs):
        if "releases/latest" in url:
            return 404, b"{}", {}
        return 200, _json.dumps({
            "id": 9000, "full_name": "newowner/moodle-mod_moved",
            "pushed_at": "2026-07-01T00:00:00Z", "stargazers_count": 1,
            "forks_count": 0, "open_issues_count": 0, "archived": False,
        }).encode(), {}

    monkeypatch.setattr(scan_mod, "_request", fake_request)
    failed = scan_mod.refresh_metrics(index, ["mod_moved"], token="x",
                                      log=lambda *a: None)
    assert failed == []
    got = _yaml.safe_load(
        (index / "plugins" / "mod" / "mod_moved.yml").read_text())
    assert got["source-repo-id"] == 9000
    keys = list(got.keys())
    assert keys.index("source-repo-id") == keys.index("source") + 1


def test_entry_schema_accepts_and_types_source_repo_id(index_dir):
    import yaml as _yaml

    from camp.validate import validate_entry

    entry_path = index_dir / "plugins" / "mod" / "mod_anchor.yml"
    entry = _claimed("mod_anchor", "https://github.com/u/moodle-mod_anchor",
                     **{"source-repo-id": 123456})
    entry_path.write_text(_yaml.safe_dump(entry, sort_keys=False))
    assert validate_entry(entry_path) == []

    entry["source-repo-id"] = "123456"
    entry_path.write_text(_yaml.safe_dump(entry, sort_keys=False))
    assert any("source-repo-id" in p for p in validate_entry(entry_path))


def test_entry_records_dependencies(tmp_path):
    """A Tier 0 entry carries dependencies observed from the same
    version.php fetch that yielded the component name (camp-tools#20)."""
    text = ("<?php\n$plugin->component = 'mod_x';\n"
            "$plugin->dependencies = ['mod_forum' => 2024042200, "
            "'local_helper' => ANY_VERSION];\n")
    entry = _entry_for(_candidate(), "mod_x", "2026-07-31", version_text=text)
    assert entry["dependencies"] == {"mod_forum": 2024042200,
                                     "local_helper": "any"}

    from camp.validate import validate_entry
    entry_dir = tmp_path / "plugins" / "mod"
    entry_dir.mkdir(parents=True)
    entry_path = entry_dir / "mod_x.yml"
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))
    assert validate_entry(entry_path) == []


def test_entry_omits_dependencies_when_none():
    text = "<?php\n$plugin->component = 'mod_x';\n$plugin->version = 1;\n"
    entry = _entry_for(_candidate(), "mod_x", "2026-07-31", version_text=text)
    assert "dependencies" not in entry


def test_site_renders_depends_on_row(index_dir, tmp_path):
    """The Depends on row links listed dependencies to their camp page and
    marks unlisted ones plainly (camp-tools#20)."""
    from camp.site import generate as site_generate

    entry_path = index_dir / "plugins" / "mod" / "mod_example.yml"
    entry = yaml.safe_load(entry_path.read_text())
    entry["releases"][0]["dependencies"] = {
        "mod_listed": 2024042200, "local_unlisted": "any"}
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))

    dep_entry = {
        "component": "mod_listed",
        "source": "https://github.com/o/moodle-mod_listed",
        "maintainers": [{"github": "o"}],
        "tier": 0, "status": "active", "releases": [],
    }
    (index_dir / "plugins" / "mod" / "mod_listed.yml").write_text(
        yaml.safe_dump(dep_entry, sort_keys=False))

    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "Depends on" in html
    assert '<a class="mono" href="/plugin/mod_listed.html">mod_listed</a>' in html
    assert "2024042200 or newer" in html
    assert "local_unlisted</span> · not in the archive" in html
    assert "declared in version.php at the latest release" in html


def test_site_depends_on_row_tier0_entry_level(index_dir, tmp_path):
    from camp.site import generate as site_generate

    entry = {
        "component": "mod_disc",
        "source": "https://github.com/o/moodle-mod_disc",
        "maintainers": [{"github": "o"}],
        "tier": 0, "status": "active", "releases": [],
        "dependencies": {"mod_forum": "any"},
    }
    (index_dir / "plugins" / "mod" / "mod_disc.yml").write_text(
        yaml.safe_dump(entry, sort_keys=False))
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_disc.html").read_text()
    assert "Depends on" in html
    assert "on the default branch" in html
    assert "or newer" not in html


def test_site_no_depends_on_row_when_none(index_dir, tmp_path):
    from camp.site import generate as site_generate
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    assert "Depends on" not in (out / "plugin" / "mod_example.html").read_text()


def test_enrich_observes_and_clears_dependencies(tmp_path, monkeypatch):
    """The enrich cycle backfills entry-level dependencies from the default
    branch for entries whose newest release record lacks the field, and
    clears a stale observation when the declaration goes away
    (camp-tools#20)."""
    import json as _json

    import camp.scan as scan_mod

    index = tmp_path / "index"
    d = index / "plugins" / "mod"
    d.mkdir(parents=True)
    # stale-dated metrics so needs_metrics triggers without force
    (d / "mod_dep.yml").write_text(yaml.safe_dump({
        "component": "mod_dep", "source": "https://github.com/u/mod_dep",
        "maintainers": [{"github": "u"}], "tier": 0, "status": "active",
        "releases": []}, sort_keys=False))
    (d / "mod_stale.yml").write_text(yaml.safe_dump({
        "component": "mod_stale", "source": "https://github.com/u/mod_stale",
        "maintainers": [{"github": "u"}], "tier": 0, "status": "active",
        "releases": [], "dependencies": {"mod_gone": "any"}}, sort_keys=False))

    def fake_request(url, token, log=print, **kwargs):
        if "releases/latest" in url:
            return 404, b"{}", {}
        if url.endswith("contents/version.php"):
            if "mod_dep" in url:
                return 200, (b"<?php\n$plugin->component = 'mod_dep';\n"
                             b"$plugin->dependencies = "
                             b"['mod_forum' => 2024042200];\n"), {}
            return 200, b"<?php\n$plugin->component = 'mod_stale';\n", {}
        name = url.rsplit("/", 1)[1]
        return 200, _json.dumps({
            "full_name": f"u/{name}", "pushed_at": "2026-07-01T00:00:00Z",
            "stargazers_count": 0, "forks_count": 0, "open_issues_count": 0,
            "archived": False}).encode(), {}

    monkeypatch.setattr(scan_mod, "_request", fake_request)
    stats = scan_mod.enrich(index, token="x", readme=False, log=lambda *a: None)
    assert stats["dependencies"] == 2
    dep = yaml.safe_load((d / "mod_dep.yml").read_text())
    assert dep["dependencies"] == {"mod_forum": 2024042200}
    stale = yaml.safe_load((d / "mod_stale.yml").read_text())
    assert "dependencies" not in stale


def test_enrich_skips_dependency_fetch_when_ledger_has_it(tmp_path, monkeypatch):
    import json as _json

    import camp.scan as scan_mod

    index = tmp_path / "index"
    d = index / "plugins" / "mod"
    d.mkdir(parents=True)
    (d / "mod_led.yml").write_text(yaml.safe_dump({
        "component": "mod_led", "source": "https://github.com/u/mod_led",
        "maintainers": [{"github": "u"}], "tier": 2,
        "security-contact": "s@example.org", "labels": ["fully-free"],
        "status": "active",
        "releases": [{"version": "1.0.0", "tag": "v1", "commit": "a" * 40,
                      "moodle-version": 2026010100, "supported-moodle": ["5.0"],
                      "zip-sha256": "b" * 64,
                      "published": "2026-01-01T00:00:00Z",
                      "dependencies": {"mod_forum": "any"}}]}, sort_keys=False))

    calls = []

    def fake_request(url, token, log=print, **kwargs):
        calls.append(url)
        if "releases/latest" in url:
            return 404, b"{}", {}
        name = url.rsplit("/", 1)[1]
        return 200, _json.dumps({
            "full_name": f"u/{name}", "pushed_at": "2026-07-01T00:00:00Z",
            "stargazers_count": 0, "forks_count": 0, "open_issues_count": 0,
            "archived": False}).encode(), {}

    monkeypatch.setattr(scan_mod, "_request", fake_request)
    scan_mod.enrich(index, token="x", readme=False, log=lambda *a: None)
    assert not any("contents/version.php" in u for u in calls)


def test_site_depends_on_core_component_wording(index_dir, tmp_path):
    """Dependencies Moodle bundles say so instead of "not in the archive",
    judged against the plugin's supported range (camp-tools#25)."""
    from camp.site import generate as site_generate

    entry_path = index_dir / "plugins" / "mod" / "mod_example.yml"
    entry = yaml.safe_load(entry_path.read_text())
    # fixture release supports 4.5 + 5.0: theme_boost standard on both,
    # mod_chat splits at 5.0, bootstrapbase left core before the window
    entry["releases"][0]["dependencies"] = {
        "theme_boost": "any", "mod_chat": 2020061500,
        "theme_bootstrapbase": "any", "local_notcore": "any"}
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))

    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "theme_boost</span> · ships with Moodle</div>" in html
    # unlisted here: "separate install" would promise availability the
    # archive can't offer, so the row states the history instead (the
    # listed variant is covered by the composes test below)
    assert ("mod_chat</span> · 2020061500 or newer · shipped with Moodle "
            "up to 4.5 · removed in 5.0") in html
    assert ("theme_bootstrapbase</span> · shipped with Moodle up to 3.6 "
            "· removed in 3.7") in html
    assert "local_notcore</span> · not in the archive" in html


def test_site_depends_on_listed_and_unbundled_composes(index_dir, tmp_path):
    """A dependency both listed in camp and formerly bundled gets the link
    AND the core note (the mod_chat case: listed at Tier 0 from the
    moodlehq legacy repo)."""
    from camp.site import generate as site_generate

    entry_path = index_dir / "plugins" / "mod" / "mod_example.yml"
    entry = yaml.safe_load(entry_path.read_text())
    entry["releases"][0]["dependencies"] = {"mod_chat": "any"}
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))
    chat = {"component": "mod_chat",
            "source": "https://github.com/moodlehq/moodle-mod_chat",
            "maintainers": [{"github": "moodlehq"}],
            "tier": 0, "status": "active", "releases": []}
    (index_dir / "plugins" / "mod" / "mod_chat.yml").write_text(
        yaml.safe_dump(chat, sort_keys=False))

    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    html = (out / "plugin" / "mod_example.html").read_text()
    assert ('<a class="mono" href="/plugin/mod_chat.html">mod_chat</a>'
            " · ships with Moodle up to 4.5 · separate install from 5.0") in html


# --- unknown-plugin-type gate (camp-tools#16) --------------------------------

CUSTOMCERT_PHP = ("<?php\n// GNU General Public License version 3\n"
                  "$plugin->component = 'customcertelement_foo';\n"
                  "$plugin->dependencies = ['mod_customcert' => 2024042200];\n")


def _type_gate_index(tmp_path):
    index = tmp_path / "index"
    (index / "plugins" / "mod").mkdir(parents=True)
    (index / "plugins" / "mod" / "mod_customcert.yml").write_text(
        "component: mod_customcert\n")
    return index


def test_scan_parks_unknown_type_prefix(tmp_path, monkeypatch):
    """First contact with an unknown subplugin family goes to needs-review
    with parent evidence and no entry written; the ledger record carries
    the component so the family report can group members by prefix."""
    import camp.scan as scan
    index = _type_gate_index(tmp_path)
    candidate = _candidate(full_name="o/moodle-customcertelement_foo",
                           html_url="https://github.com/o/moodle-customcertelement_foo")
    monkeypatch.setattr(scan, "_search", lambda *a, **k: ([candidate], 1))
    monkeypatch.setattr(scan, "_fetch_component",
                        lambda c, t, log=None: ("ok", "customcertelement_foo",
                                                CUSTOMCERT_PHP))

    results = scan.scan(index, queries=["x"], limit=1, token="fake")
    assert results[0].outcome == "needs-review"
    assert not (index / "plugins" / "customcertelement").exists()
    record = scan.load_ledger(index)["o/moodle-customcertelement_foo"]
    assert record["component"] == "customcertelement_foo"
    assert record["detail"].startswith("unknown plugin type 'customcertelement'")
    assert "declares dependency on listed mod_customcert" in record["detail"]

    from camp.scan import unknown_type_families
    families = unknown_type_families(index)
    assert list(families) == ["customcertelement"]
    assert families["customcertelement"][0][0] == "o/moodle-customcertelement_foo"


def test_scan_lists_established_family_member(tmp_path, monkeypatch):
    """Once the family is recorded in discovery/subplugin-families.yml the
    same member lists like any known type, and the family report is empty
    even while old parked records remain in the ledger."""
    import camp.scan as scan
    index = _type_gate_index(tmp_path)
    families_file = index / "discovery" / "subplugin-families.yml"
    families_file.parent.mkdir(parents=True)
    families_file.write_text(
        "customcertelement:\n"
        "  parent: mod_customcert\n"
        "  name: Certificate elements\n")
    candidate = _candidate(full_name="o/moodle-customcertelement_foo",
                           html_url="https://github.com/o/moodle-customcertelement_foo")
    monkeypatch.setattr(scan, "_search", lambda *a, **k: ([candidate], 1))
    monkeypatch.setattr(scan, "_fetch_component",
                        lambda c, t, log=None: ("ok", "customcertelement_foo",
                                                CUSTOMCERT_PHP))

    results = scan.scan(index, queries=["x"], limit=1, token="fake")
    assert results[0].outcome == "written"
    assert (index / "plugins" / "customcertelement"
            / "customcertelement_foo.yml").exists()
    from camp.scan import unknown_type_families
    assert unknown_type_families(index) == {}


def test_unknown_type_families_ignores_name_mismatch_records(tmp_path):
    """Only unknown-type records feed the family queue; the long-standing
    name-mismatch needs-review class carries no component and stays out."""
    import camp.scan as scan
    index = tmp_path / "index"
    (index / "discovery").mkdir(parents=True)
    ledger = {}
    scan.record_outcome(ledger, _candidate(full_name="o/weird-name"),
                        "needs-review",
                        "declares mod_x but repo name does not correspond; "
                        "human sign-off required before listing (RFC §8)",
                        "2026-08-05")
    scan.record_outcome(ledger, _candidate(full_name="o/moodle-floreamui_a"),
                        "needs-review",
                        "unknown plugin type 'floreamui'; family establishment "
                        "review required before listing (camp-tools#16)",
                        "2026-08-05", component="floreamui_a")
    scan.save_ledger(index, ledger)
    families = scan.unknown_type_families(index)
    assert list(families) == ["floreamui"]
