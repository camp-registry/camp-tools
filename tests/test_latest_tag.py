"""Newest upstream tag beside the GitHub Release (camp-tools#59): enrich
records it from one GraphQL query; the site shows whichever is newer."""

import json

import yaml

import camp.scan as scan
from camp.scan import _fetch_latest_tag, _fetch_metrics
from camp.site import _upstream_newest, _upstream_url
from camp.validate import validate_entry


def _graphql_payload(name, date, annotated=False):
    target = ({"target": {"committedDate": date}} if annotated
              else {"committedDate": date})
    return {"repository": {"refs": {"nodes": [{"name": name, "target": target}]}}}


def test_fetch_latest_tag_lightweight_and_annotated(monkeypatch):
    seen = []
    monkeypatch.setattr(scan, "_graphql",
                        lambda q, v, t: (seen.append(v), _graphql_payload("v5.2-r9", "2026-09-18T06:30:51Z"))[1])
    assert _fetch_latest_tag("o/moodle-x", "tok") == {"tag": "v5.2-r9", "date": "2026-09-18T06:30:51Z"}
    assert seen == [{"owner": "o", "name": "moodle-x"}]
    monkeypatch.setattr(scan, "_graphql",
                        lambda q, v, t: _graphql_payload("V501.1.5", "2026-09-26T11:00:46Z", annotated=True))
    assert _fetch_latest_tag("o/moodle-x", "tok")["date"] == "2026-09-26T11:00:46Z"


def test_fetch_latest_tag_skips_moving_tags_without_a_digit(monkeypatch):
    payload = {"repository": {"refs": {"nodes": [
        {"name": "phar-latest", "target": {"committedDate": "2026-04-08T14:01:48Z"}},
        {"name": "stable", "target": {"committedDate": "2026-04-01T00:00:00Z"}},
        {"name": "4.5.11", "target": {"committedDate": "2026-03-07T10:05:11Z"}},
    ]}}}
    monkeypatch.setattr(scan, "_graphql", lambda q, v, t: payload)
    assert _fetch_latest_tag("moosh/moosh", "tok") == {"tag": "4.5.11", "date": "2026-03-07T10:05:11Z"}
    only_moving = {"repository": {"refs": {"nodes": [
        {"name": "latest", "target": {"committedDate": "2026-04-08T14:01:48Z"}}]}}}
    monkeypatch.setattr(scan, "_graphql", lambda q, v, t: only_moving)
    assert _fetch_latest_tag("o/r", "tok") is None


def test_fetch_latest_tag_handles_no_tags_errors_and_bad_paths(monkeypatch):
    monkeypatch.setattr(scan, "_graphql", lambda q, v, t: {"repository": {"refs": {"nodes": []}}})
    assert _fetch_latest_tag("o/moodle-x", "tok") is None
    monkeypatch.setattr(scan, "_graphql", lambda q, v, t: None)
    assert _fetch_latest_tag("o/moodle-x", "tok") is None
    assert _fetch_latest_tag("not-a-path", "tok") is None
    assert _fetch_latest_tag("o/x/y", "tok") is None


def test_metrics_carry_latest_tag_only_with_a_token(monkeypatch):
    def fake_request(url, token, **kw):
        if "releases/latest" in url:
            return 404, b"{}", {}
        return 200, json.dumps({"full_name": "u/x", "pushed_at": "2026-07-01T00:00:00Z",
                                "stargazers_count": 1, "forks_count": 0,
                                "open_issues_count": 0, "archived": False}).encode(), {}
    monkeypatch.setattr(scan, "_request", fake_request)
    calls = []

    def fake_graphql(q, v, t):
        calls.append(t)
        return _graphql_payload("v1.2", "2026-08-01T00:00:00Z") if t else None
    monkeypatch.setattr(scan, "_graphql", fake_graphql)
    status, metrics, _ = _fetch_metrics("https://github.com/u/x", "tok", "2026-09-30", log=lambda *a: None)
    assert status == "ok" and metrics["latest-tag"] == {"tag": "v1.2", "date": "2026-08-01T00:00:00Z"}
    assert "latest-release" not in metrics
    status, metrics, _ = _fetch_metrics("https://github.com/u/x", None, "2026-09-30", log=lambda *a: None)
    assert status == "ok" and "latest-tag" not in metrics
    # key order: latest-tag sits before checked like latest-release does
    status, metrics, _ = _fetch_metrics("https://github.com/u/x", "tok", "2026-09-30", log=lambda *a: None)
    assert list(metrics)[-2:] == ["latest-tag", "checked"]


def test_upstream_newest_prefers_the_newer_and_release_on_ties():
    rel = {"tag": "v4.0.0", "date": "2022-04-20T17:46:11Z"}
    tag = {"tag": "v5.0.10", "date": "2026-09-15T10:07:12Z"}
    assert _upstream_newest({"latest-release": rel, "latest-tag": tag}) == ("tag", tag)
    newer_rel = {"tag": "v6", "date": "2026-10-01T00:00:00Z"}
    assert _upstream_newest({"latest-release": newer_rel, "latest-tag": tag}) == ("release", newer_rel)
    assert _upstream_newest({"latest-release": {"tag": "v1"}, "latest-tag": tag}) == ("release", {"tag": "v1"})
    assert _upstream_newest({"latest-tag": tag}) == ("tag", tag)
    assert _upstream_newest({"latest-release": rel}) == ("release", rel)
    assert _upstream_newest({}) == ("", {})


def test_upstream_url_per_host():
    assert _upstream_url("https://github.com/o/r", "tag", "v5.2-r9") == "https://github.com/o/r/releases/tag/v5.2-r9"
    assert _upstream_url("https://gitlab.com/g/p/", "tag", "v1/2") == "https://gitlab.com/g/p/-/tags/v1%2F2"


def test_entry_with_latest_tag_validates(tmp_path):
    path = tmp_path / "plugins" / "local" / "local_x.yml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({
        "component": "local_x", "source": "https://github.com/h/moodle-local_x",
        "maintainers": [{"github": "h"}], "tier": 0, "releases": [],
        "metrics": {"stars": 0, "forks": 0, "open-issues": 0, "archived": False,
                    "latest-tag": {"tag": "v1.0", "date": "2026-09-01T00:00:00Z"},
                    "checked": "2026-09-30"}}, sort_keys=False))
    assert validate_entry(path) == []


def test_upstream_newest_lets_a_newer_release_record_win():
    """camp-tools#74: the metrics tag lags a verified release by up to two
    weeks; the newest release record wins when strictly newer."""
    tag = {"tag": "v5.2.3.03", "date": "2026-09-18T21:38:43Z"}
    entry = {"releases": [
        {"tag": "v5.1.8.01", "released": "2026-10-04T14:00:00Z"},
        {"tag": "v5.2.4.01", "released": "2026-10-04T14:53:51Z"},
        {"tag": "v0.1", "published": "2020-01-01T00:00:00Z"}]}
    assert _upstream_newest({"latest-tag": tag}, entry) == (
        "record", {"tag": "v5.2.4.01", "date": "2026-10-04T14:53:51Z"})
    # the host's tag is newer: it keeps the row
    newer = {"tag": "v5.3.0", "date": "2026-10-06T00:00:00Z"}
    assert _upstream_newest({"latest-tag": newer}, entry) == ("tag", newer)
    # no metrics at all: the record still fills the row
    assert _upstream_newest({}, entry)[0] == "record"
    # no entry: unchanged behaviour
    assert _upstream_newest({"latest-tag": tag}) == ("tag", tag)


def test_labels_follow_the_released_listing():
    """camp-tools#73: the listing manifest's labels win over the entry copy."""
    from camp.site import labels_for
    entry = {"labels": ["fully-free"]}
    assert labels_for(entry, {"labels": ["fully-free", "requires-core-patch"]}) == [
        "fully-free", "requires-core-patch"]
    assert labels_for(entry, {}) == ["fully-free"]
    assert labels_for(entry, {"labels": []}) == ["fully-free"]
    assert labels_for(entry, None) == ["fully-free"]
    assert labels_for({}, {"labels": "oops"}) == []
