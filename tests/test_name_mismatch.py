"""Name mismatches are rejected like any other gate, with the fix in the
detail; a seed request lifts the gate for one targeted run; the legacy
needs-review records reclassify mechanically (camp-tools#60)."""

import yaml

import camp.scan as scan
from camp.scan import (Candidate, load_ledger, name_mismatch_detail,
                       reclassify_mismatches, save_ledger)

VERSION_PHP = ("<?php\n// it under the terms of the GNU General Public License as\n"
               "// published by the Free Software Foundation, either version 3.\n"
               "$plugin->component = 'theme_ecampus';\n")


def _candidate(full_name):
    return Candidate(full_name=full_name, html_url=f"https://github.com/{full_name}",
                     owner=full_name.split("/")[0], description="", license_spdx="GPL-3.0",
                     stars=0, default_branch="main", archived=False)


def _index(tmp_path):
    index = tmp_path / "index"
    (index / "plugins").mkdir(parents=True)
    (index / "discovery").mkdir()
    return index


def _scan(monkeypatch, index, full_name, **kw):
    monkeypatch.setattr(scan, "_search", lambda *a, **k: ([_candidate(full_name)], 1))
    monkeypatch.setattr(scan, "_fetch_component",
                        lambda c, t, log=None: ("ok", "theme_ecampus", VERSION_PHP))
    return scan.scan(index, queries=["x"], limit=1, token="fake", **kw)


def test_mismatch_is_rejected_with_the_fix_recorded(tmp_path, monkeypatch):
    index = _index(tmp_path)
    results = _scan(monkeypatch, index, "00209317/TESIS-Moodle")
    assert results[0].outcome == "name-mismatch"
    assert not (index / "plugins" / "theme" / "theme_ecampus.yml").exists()
    record = load_ledger(index)["00209317/TESIS-Moodle"]
    assert record["outcome"] == "name-mismatch"
    assert record["component"] == "theme_ecampus"
    assert "rename the repository" in record["detail"] and "seed request" in record["detail"]
    assert "'theme_ecampus'" in record["detail"] and "'ecampus'" in record["detail"]


def test_allow_mismatch_lists_the_repository(tmp_path, monkeypatch):
    index = _index(tmp_path)
    results = _scan(monkeypatch, index, "00209317/TESIS-Moodle", allow_mismatch=True)
    assert results[0].outcome == "written"
    assert (index / "plugins" / "theme" / "theme_ecampus.yml").exists()


def test_name_mismatch_detail_names_both_forms():
    d = name_mismatch_detail("mod_attendance")
    assert "'mod_attendance'" in d and "'attendance'" in d and "camp-tools#60" in d


LEGACY = ("declares {c} but repo name does not correspond; human sign-off "
          "required before listing (RFC §8)")


def _legacy(component):
    return {"outcome": "needs-review", "detail": LEGACY.format(c=component),
            "first-seen": "2026-07-11", "last-checked": "2026-07-11"}


def test_reclassify_applies_the_mechanical_rules(tmp_path, monkeypatch):
    index = _index(tmp_path)
    # a listed component held by one repo, used by two of the records
    held = index / "plugins" / "local" / "local_held.yml"
    held.parent.mkdir(parents=True)
    held.write_text(yaml.safe_dump({"component": "local_held",
                                    "source": "https://github.com/holder/odd-name"}))
    ledger = {
        "gone/moodle-thing": _legacy("mod_gone"),
        "holder/odd-name": _legacy("local_held"),           # listed here: stale park
        "rival/other-name": _legacy("local_held"),          # listed elsewhere: classify
        "anchored/strange-repo": _legacy("block_anchored"), # old directory vouched
        "someone/TESIS-Moodle": _legacy("theme_ecampus"),   # plain rejection
        "x/moodle-unknowntype_a": {"outcome": "needs-review",
                                   "detail": "unknown plugin type 'unknowntype'; family establishment review required before listing (camp-tools#16)",
                                   "first-seen": "2026-07-11", "last-checked": "2026-07-11",
                                   "component": "unknowntype_a"},
    }
    save_ledger(index, ledger)
    monkeypatch.setattr(scan, "_request",
                        lambda url, token, **kw: ((404, b"{}", {}) if "gone/" in url else (200, b"{}", {})))
    monkeypatch.setattr(scan, "classify_existing",
                        lambda idx, url, comp, tok: ("name-collision", f"independent repository declaring {comp}"))
    from camp import directorymap
    monkeypatch.setattr(directorymap, "directory_source",
                        lambda comp: "https://github.com/anchored/strange-repo" if comp == "block_anchored" else None)
    monkeypatch.setattr(directorymap, "same_repo", lambda a, b: a.lower() == b.lower())

    stats = reclassify_mismatches(index, token="t", log=lambda *a: None)
    after = load_ledger(index)
    assert stats["pruned"] == ["gone/moodle-thing"] and "gone/moodle-thing" not in after
    assert stats["stale"] == ["holder/odd-name"] and "holder/odd-name" not in after
    assert after["rival/other-name"]["outcome"] == "name-collision"
    assert after["rival/other-name"]["component"] == "local_held"
    assert stats["anchored"] == [("anchored/strange-repo", "block_anchored")]
    assert after["anchored/strange-repo"]["outcome"] == "needs-review"  # left for --allow-mismatch
    assert after["someone/TESIS-Moodle"]["outcome"] == "name-mismatch"
    assert after["someone/TESIS-Moodle"]["component"] == "theme_ecampus"
    assert "rename the repository" in after["someone/TESIS-Moodle"]["detail"]
    assert after["x/moodle-unknowntype_a"]["outcome"] == "needs-review"  # untouched
    assert stats["seen"] == 5 and stats["rejected"] == 1


def test_reclassify_dry_run_writes_nothing(tmp_path, monkeypatch):
    index = _index(tmp_path)
    save_ledger(index, {"someone/TESIS-Moodle": _legacy("theme_ecampus")})
    monkeypatch.setattr(scan, "_request", lambda url, token, **kw: (200, b"{}", {}))
    stats = reclassify_mismatches(index, token="t", dry_run=True, log=lambda *a: None)
    assert stats["rejected"] == 1
    assert load_ledger(index)["someone/TESIS-Moodle"]["outcome"] == "needs-review"
