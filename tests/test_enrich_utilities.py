"""utilities/ enrich sweep (camp-docs#4): repo metrics for open tools,
checked-stamp-only for closed-source, channel releases, rename handling."""

import yaml

from camp import scan


def _index(tmp_path, entry):
    d = tmp_path / "utilities"
    d.mkdir(parents=True)
    (d / f'{entry["name"]}.yml').write_text(yaml.safe_dump(entry))
    return tmp_path


def _entry(**overrides):
    entry = {
        "name": "moosh",
        "summary": "MOOdle SHell.",
        "category": "cli",
        "source": "https://github.com/tmuras/moosh",
        "source-repo-id": 6603614,
        "install": ["composer"],
        "license": "GPL-3.0",
        "first-seen": "2026-08-12",
    }
    entry.update(overrides)
    return entry


def _read(tmp_path, name="moosh"):
    return yaml.safe_load((tmp_path / "utilities" / f"{name}.yml").read_text())


def _ok_metrics(monkeypatch, canonical=None):
    monkeypatch.setattr(scan, "_fetch_metrics", lambda source, token, checked, log: (
        "ok", {"updated": "2026-08-01", "stars": 10, "forks": 2,
               "open-issues": 1, "archived": False,
               "latest-release": {"tag": "1.0", "date": "2026-07-01"},
               "checked": checked}, canonical))


def test_open_tool_gets_repo_metrics(tmp_path, monkeypatch):
    _ok_metrics(monkeypatch)
    index = _index(tmp_path, _entry())
    stats = scan.enrich_utilities(index, log=lambda *_: None)
    assert stats["metrics"] == 1
    metrics = _read(tmp_path)["metrics"]
    assert metrics["stars"] == 10
    assert metrics["latest-release"]["tag"] == "1.0"


def test_closed_source_gets_no_repo_metrics(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("closed-source must not fetch repo metrics")
    monkeypatch.setattr(scan, "_fetch_metrics", boom)
    monkeypatch.setattr(scan, "fetch_channel_release",
                        lambda channel, token=None: {"tag": "1.6.4",
                                                     "url": "https://x",
                                                     "date": "2026-08-04"})
    index = _index(tmp_path, _entry(
        name="mdlcode", **{"closed-source": True,
                           "release-channel": "openvsx:ns/ext"}))
    scan.enrich_utilities(index, log=lambda *_: None)
    metrics = _read(tmp_path, "mdlcode")["metrics"]
    assert "stars" not in metrics
    assert metrics["latest-release"]["tag"] == "1.6.4"
    assert metrics["checked"]
    # release date doubles as `updated`: the only observable activity,
    # so health and recency render for closed-source entries too
    assert metrics["updated"] == "2026-08-04"


def test_channel_error_keeps_last_known_release(tmp_path, monkeypatch):
    monkeypatch.setattr(scan, "fetch_channel_release",
                        lambda channel, token=None: None)
    index = _index(tmp_path, _entry(
        name="mdlcode",
        metrics={"latest-release": {"tag": "1.6.3"}, "checked": "2026-08-01"},
        **{"closed-source": True, "release-channel": "openvsx:ns/ext"}))
    scan.enrich_utilities(index, log=lambda *_: None)
    assert _read(tmp_path, "mdlcode")["metrics"]["latest-release"]["tag"] == "1.6.3"


def test_channel_overrides_repo_release(tmp_path, monkeypatch):
    _ok_metrics(monkeypatch)
    monkeypatch.setattr(scan, "fetch_channel_release",
                        lambda channel, token=None: {"tag": "2.0"})
    index = _index(tmp_path, _entry(**{"release-channel": "openvsx:ns/ext"}))
    scan.enrich_utilities(index, log=lambda *_: None)
    assert _read(tmp_path)["metrics"]["latest-release"]["tag"] == "2.0"


def test_curated_rename_updates_source_claimed_flags(tmp_path, monkeypatch):
    _ok_metrics(monkeypatch, canonical="https://github.com/new/home")
    index = _index(tmp_path, _entry())
    scan.enrich_utilities(index, log=lambda *_: None)
    assert _read(tmp_path)["source"] == "https://github.com/new/home"

    _ok_metrics(monkeypatch, canonical="https://github.com/new/home")
    index2 = _index(tmp_path / "second", _entry(
        claimed="2026-08-13", maintainers=[{"github": "tmuras"}]))
    scan.enrich_utilities(index2, log=lambda *_: None)
    entry = _read(tmp_path / "second")
    assert entry["source"] == "https://github.com/tmuras/moosh"
    assert entry["metrics"]["renamed-to"] == "https://github.com/new/home"


def test_enrich_hook_covers_utilities(tmp_path, monkeypatch):
    _ok_metrics(monkeypatch)
    (tmp_path / "plugins").mkdir()
    _index(tmp_path, _entry())
    stats = scan.enrich(tmp_path, readme=False, log=lambda *_: None)
    assert stats["utilities"] == 1
