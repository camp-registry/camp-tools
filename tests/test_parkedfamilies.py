"""Parked family reviews: gate re-check (camp-tools#49)."""

import json

import pytest

from camp import parkedfamilies as pf
from camp.cli import main


def _index(tmp_path, parked: dict | None, listed=()):
    for component in listed:
        d = tmp_path / "plugins" / component.partition("_")[0]
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{component}.yml").write_text(f"component: {component}\n")
    if parked is not None:
        import yaml
        (tmp_path / "discovery").mkdir(exist_ok=True)
        (tmp_path / pf.PARKED_PATH).write_text(yaml.safe_dump(parked))
    return tmp_path


def _fetcher(declarations: dict, status_for: dict | None = None):
    """declarations: {owner/repo: {prefix: path}}; repos absent are 404,
    status_for overrides the HTTP status for a repo."""
    calls = []

    def fetch(url):
        calls.append(url)
        repo = url.removeprefix(pf._RAW + "/").removesuffix("/HEAD/db/subplugins.json")
        if status_for and repo in status_for:
            return status_for[repo], ""
        if repo not in declarations:
            return 404, ""
        return 200, json.dumps({"plugintypes": declarations[repo]})
    fetch.calls = calls
    return fetch


RECORD = {"parent": "local_x", "watch": "o/moodle-local_x",
          "issue": "https://github.com/camp-registry/camp-index/issues/1"}


def test_missing_file_means_nothing_parked(tmp_path):
    assert pf.check(_index(tmp_path, None), fetch=_fetcher({})) == []


def test_malformed_record_raises(tmp_path):
    idx = _index(tmp_path, {"xtype": {"parent": "local_x"}})
    with pytest.raises(ValueError, match="needs a mapping with parent, watch, issue"):
        pf.load_parked(idx)


def test_parked_when_nothing_declares(tmp_path):
    idx = _index(tmp_path, {"xtype": RECORD}, listed=["local_x"])
    [s] = pf.check(idx, fetch=_fetcher({}))
    assert s.state == "parked" and not s.flipped and s.listed


def test_flipped_when_watched_repo_declares_and_parent_listed(tmp_path):
    idx = _index(tmp_path, {"xtype": RECORD}, listed=["local_x"])
    fetch = _fetcher({"o/moodle-local_x": {"xtype": "local/x/xtype"}})
    [s] = pf.check(idx, fetch=fetch)
    assert s.flipped and s.state == "flipped"
    assert s.declared_by == ["o/moodle-local_x"]
    assert pf.tsv_line(s).split("\t") == [
        "xtype", "local_x", RECORD["issue"], "o/moodle-local_x", "flipped"]


def test_declared_but_parent_unlisted_is_not_flipped(tmp_path):
    idx = _index(tmp_path, {"xtype": RECORD})  # no listing for local_x
    fetch = _fetcher({"o/moodle-local_x": {"xtype": "local/x/xtype"}})
    [s] = pf.check(idx, fetch=fetch)
    assert s.state == "declared-unlisted" and not s.flipped


def test_subplugintypes_spelling_and_other_prefixes_ignored(tmp_path):
    idx = _index(tmp_path, {"xtype": RECORD}, listed=["local_x"])

    def fetch(url):
        return 200, json.dumps({"subplugintypes": {"other": "other"},
                                "plugintypes": {"other": "local/x/other"}})
    [s] = pf.check(idx, fetch=fetch)
    assert s.state == "parked"


def test_also_watch_string_or_list(tmp_path):
    rec = {**RECORD, "also-watch": ["f/moodle-local_x", "g/moodle-local_x"]}
    idx = _index(tmp_path, {"xtype": rec}, listed=["local_x"])
    fetch = _fetcher({"g/moodle-local_x": {"xtype": "p"}})
    [s] = pf.check(idx, fetch=fetch)
    assert s.flipped and s.declared_by == ["g/moodle-local_x"]
    assert len(fetch.calls) == 3
    assert pf.watched_repos({**RECORD, "also-watch": "f/r"}) == ["o/moodle-local_x", "f/r"]


def test_transient_failure_raises_rather_than_reading_as_parked(tmp_path):
    idx = _index(tmp_path, {"xtype": RECORD}, listed=["local_x"])
    with pytest.raises(RuntimeError, match="HTTP 503"):
        pf.check(idx, fetch=_fetcher({}, status_for={"o/moodle-local_x": 503}))


def test_cli_prints_only_flipped_unless_all(tmp_path, monkeypatch, capsys):
    parked = {"xtype": RECORD,
              "ytype": {**RECORD, "parent": "local_y", "watch": "o/moodle-local_y"}}
    idx = _index(tmp_path, parked, listed=["local_x", "local_y"])
    monkeypatch.setattr(pf, "_fetch", _fetcher({"o/moodle-local_x": {"xtype": "p"}}))
    assert main(["parked-families-check", str(idx)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == ["xtype\tlocal_x\t" + RECORD["issue"] + "\to/moodle-local_x\tflipped"]
    assert main(["parked-families-check", str(idx), "--all"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert [l.split("\t")[-1] for l in lines] == ["flipped", "parked"]
