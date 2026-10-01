"""Nightly ledger prune: gone and renamed repositories drop out, the rest
get a probe stamp, oldest probe first (camp-tools#61, camp-index#21)."""

import json

import camp.scan as scan
from camp.scan import ledger_prune, load_ledger, save_ledger


def _rec(outcome="no-version-php", probed=None, host=None):
    r = {"outcome": outcome, "detail": "d", "first-seen": "2026-07-01", "last-checked": "2026-07-01"}
    if probed:
        r["probed"] = probed
    if host:
        r["host"] = host
    return r


def _index(tmp_path, ledger):
    index = tmp_path / "index"
    (index / "discovery").mkdir(parents=True)
    save_ledger(index, ledger)
    return index


def _api(answers):
    calls = []

    def fake(url, token, **kw):
        calls.append(url)
        for frag, (status, body, headers) in answers.items():
            if frag in url:
                return status, json.dumps(body).encode(), headers
        raise AssertionError(f"unexpected probe: {url}")
    fake.calls = calls
    return fake


def test_gone_and_renamed_drop_out_others_get_stamped(tmp_path, monkeypatch):
    index = _index(tmp_path, {
        "a/gone": _rec(),
        "b/moved": _rec(),
        "c/alive": _rec(),
        "d/listed": _rec(outcome="written"),
        "e/optout": _rec(outcome="opted-out"),
        "g/project": _rec(host="gitlab.com"),
    })
    fake = _api({
        "repos/a/gone": (404, {}, {}),
        "repos/b/moved": (200, {"full_name": "b/new-name"}, {}),
        "repos/c/alive": (200, {"full_name": "c/alive"}, {}),
        "gitlab.com/api/v4/projects/g%2Fproject": (404, {}, {}),
    })
    monkeypatch.setattr(scan, "_request", fake)
    stats = ledger_prune(index, token="t", log=lambda *a: None)
    after = load_ledger(index)
    assert stats["gone"] == ["a/gone", "g/project"]
    assert stats["renamed"] == [("b/moved", "b/new-name")]
    assert "a/gone" not in after and "b/moved" not in after and "g/project" not in after
    assert after["c/alive"]["probed"]  # stamped today
    assert "probed" not in after["d/listed"] and "probed" not in after["e/optout"]
    assert not any("d/listed" in u or "e/optout" in u for u in fake.calls)


def test_oldest_probe_first_and_limit(tmp_path, monkeypatch):
    index = _index(tmp_path, {
        "a/new": _rec(probed="2026-09-30"),
        "b/old": _rec(probed="2026-08-01"),
        "c/never": _rec(),
    })
    fake = _api({"repos/": (200, {"full_name": "x"}, {})})
    # full_name "x" never matches, so every probed record would be "renamed";
    # the assertion is about WHICH records get probed under the limit
    monkeypatch.setattr(scan, "_request", fake)
    stats = ledger_prune(index, token="t", limit=2, log=lambda *a: None)
    assert [u.rsplit("/repos/", 1)[1] for u in fake.calls] == ["c/never", "b/old"]
    assert stats["probed"] == 2


def test_rate_limit_stops_early_and_keeps_the_rest(tmp_path, monkeypatch):
    index = _index(tmp_path, {"a/one": _rec(), "b/two": _rec()})
    fake = _api({"repos/": (403, {}, {"X-RateLimit-Remaining": "0"})})
    monkeypatch.setattr(scan, "_request", fake)
    stats = ledger_prune(index, token="t", log=lambda *a: None)
    assert stats["rate-limited"] and stats["probed"] == 0
    assert set(load_ledger(index)) == {"a/one", "b/two"}


def test_transient_error_keeps_the_record(tmp_path, monkeypatch):
    index = _index(tmp_path, {"a/flaky": _rec()})
    monkeypatch.setattr(scan, "_request", _api({"repos/": (0, {}, {})}))
    stats = ledger_prune(index, token="t", log=lambda *a: None)
    assert stats["kept"] == 1 and "a/flaky" in load_ledger(index)


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    index = _index(tmp_path, {"a/gone": _rec()})
    monkeypatch.setattr(scan, "_request", _api({"repos/": (404, {}, {})}))
    stats = ledger_prune(index, token="t", dry_run=True, log=lambda *a: None)
    assert stats["gone"] == ["a/gone"] and "a/gone" in load_ledger(index)
