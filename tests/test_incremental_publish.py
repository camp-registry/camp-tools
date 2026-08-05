"""Incremental publish: the previously published site is the cache."""

import json

import yaml

from camp.checks import CHECKER_VERSION, run_checks
from camp.ingest import ingest_all


def _entry(entry_path):
    return yaml.safe_load(entry_path.read_text())


# ---- checks reuse ----------------------------------------------------------

def _prior_checks(tmp_path, commit, checker=CHECKER_VERSION):
    prior = tmp_path / "prior-checks"
    prior.mkdir()
    (prior / "mod_example.json").write_text(json.dumps({
        "component": "mod_example", "checked": "2026-01-01",
        "checker": checker,
        "versions": {"1.0.0": {"tag": "v1.0.0", "commit": commit,
                               "phplint": True, "errors": 0, "warnings": 3,
                               "files": 1, "rules": {"x.y": 3}}}}))
    return prior


def test_checks_reused_from_prior_publish(index_dir, entry_path, tmp_path):
    # no php/phpcs on the test host: only reuse can produce output, which
    # is exactly the property under test
    commit = _entry(entry_path)["releases"][0]["commit"]
    out = tmp_path / "checks"
    run_checks(index_dir, out, log=lambda *a: None,
               reuse=str(_prior_checks(tmp_path, commit)))
    doc = json.loads((out / "mod_example.json").read_text())
    assert doc["versions"]["1.0.0"]["warnings"] == 3
    assert doc["checker"] == CHECKER_VERSION


def test_checks_stale_commit_not_fully_reused(index_dir, entry_path, tmp_path):
    """A prior summary whose commit no longer matches the ledger is not
    treated as current; without php on the host nothing new is computable,
    so the release stays unchecked rather than wrongly reused."""
    out = tmp_path / "checks"
    run_checks(index_dir, out, log=lambda *a: None,
               reuse=str(_prior_checks(tmp_path, "f" * 40)))
    if (out / "mod_example.json").exists():
        doc = json.loads((out / "mod_example.json").read_text())
        assert doc["versions"].get("1.0.0", {}).get("commit") != \
            _entry(entry_path)["releases"][0]["commit"]


def test_checks_bumped_checker_invalidates_prior(index_dir, entry_path, tmp_path):
    commit = _entry(entry_path)["releases"][0]["commit"]
    stale = tmp_path / "stale"
    stale.mkdir()
    prior = _prior_checks(stale, commit, checker=CHECKER_VERSION + 1)
    out = tmp_path / "checks"
    run_checks(index_dir, out, log=lambda *a: None, reuse=str(prior))
    assert not (out / "mod_example.json").exists()


# ---- ingest-all reuse ------------------------------------------------------

def test_ingest_all_reuses_published_site(index_dir, entry_path, plugin_repo, tmp_path):
    # point the entry at the local fixture repo so run 1 can clone it
    entry = _entry(entry_path)
    entry["source"] = str(plugin_repo)
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))

    out1 = tmp_path / "listings1"
    ingested, reused = ingest_all(index_dir, out1, log=lambda *a: None)
    assert (ingested, reused) == (1, 0)
    manifest = json.loads((out1 / "manifest.json").read_text())
    assert manifest["components"]["mod_example"]["commit"] == entry["releases"][0]["commit"]

    # simulate the published site: listings/ + manifest under a base dir
    site = tmp_path / "published"
    (site / "listings").mkdir(parents=True)
    (site / "listings" / "mod_example.yml").write_bytes(
        (out1 / "mod_example.yml").read_bytes())
    (site / "listings" / "manifest.json").write_text(json.dumps(manifest))

    # run 2: source is now bogus — success proves no clone happened
    entry["source"] = str(tmp_path / "does-not-exist")
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))
    out2 = tmp_path / "listings2"
    ingested, reused = ingest_all(index_dir, out2, reuse=str(site),
                                  log=lambda *a: None)
    assert (ingested, reused) == (0, 1)
    assert yaml.safe_load((out2 / "mod_example.yml").read_text())["name"] == "Example Activity"
    assert json.loads((out2 / "manifest.json").read_text())["components"]["mod_example"]


def test_ingest_all_reuse_rejects_pin_mismatch(index_dir, entry_path, plugin_repo, tmp_path):
    """A published listing that no longer matches the ledger pin falls back
    to a source ingest (which here fails loudly on the bogus source) —
    reuse can never launder unpinned bytes."""
    entry = _entry(entry_path)
    entry["source"] = str(plugin_repo)
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))
    out1 = tmp_path / "listings1"
    ingest_all(index_dir, out1, log=lambda *a: None)
    manifest = json.loads((out1 / "manifest.json").read_text())

    site = tmp_path / "published"
    (site / "listings").mkdir(parents=True)
    (site / "listings" / "mod_example.yml").write_text(
        "name: Tampered\nsummary: s\nlabels: [fully-free]\n")
    (site / "listings" / "manifest.json").write_text(json.dumps(manifest))

    entry["source"] = str(tmp_path / "does-not-exist")
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))
    out2 = tmp_path / "listings2"
    logs = []
    ingested, reused = ingest_all(index_dir, out2, reuse=str(site),
                                  log=lambda m: logs.append(m))
    assert reused == 0
    assert not (out2 / "mod_example.yml").exists()
    assert any("pin" in m or "clone failed" in m for m in logs)


def test_amd_fileset_check(tmp_path):
    """Build-output completeness (camp-tools#4 first slice): orphan build
    files and unbuilt sources are named; complete sets pass; plugins
    without AMD report nothing."""
    from camp.checks import _amd_fileset
    root = tmp_path / "plugin"
    (root / "amd" / "src").mkdir(parents=True)
    (root / "amd" / "build").mkdir()
    (root / "amd" / "src" / "app.js").write_text("//")
    (root / "amd" / "build" / "app.min.js").write_text("//")
    (root / "amd" / "build" / "mywords.min.js").write_text("//")
    (root / "amd" / "src" / "helper.js").write_text("//")
    result = _amd_fileset(root)
    assert result["src"] == 2 and result["build"] == 2
    assert result["build_without_src"] == ["mywords"]
    assert result["src_without_build"] == ["helper"]

    clean = tmp_path / "clean"
    (clean / "amd" / "src").mkdir(parents=True)
    (clean / "amd" / "build").mkdir()
    (clean / "amd" / "src" / "app.js").write_text("//")
    (clean / "amd" / "build" / "app.min.js").write_text("//")
    ok = _amd_fileset(clean)
    assert ok["build_without_src"] == [] and ok["src_without_build"] == []

    assert _amd_fileset(tmp_path / "noamd") is None


def test_amd_chip_states():
    """Warn-only display: orphans outrank unbuilt; complete sets show a
    check mark; absent AMD renders no chip."""
    from camp.site import _check_chips
    base = {"phplint": True, "errors": 0, "warnings": 0}
    orphan = _check_chips({**base, "amd": {"src": 1, "build": 2,
                                           "src_without_build": [],
                                           "build_without_src": ["mywords"]}})
    assert "1 without source" in orphan and "mywords" in orphan
    unbuilt = _check_chips({**base, "amd": {"src": 2, "build": 1,
                                            "src_without_build": ["helper"],
                                            "build_without_src": []}})
    assert "1 not built" in unbuilt and "helper" in unbuilt
    clean = _check_chips({**base, "amd": {"src": 1, "build": 1,
                                          "src_without_build": [],
                                          "build_without_src": []}})
    assert "amd build" in clean and "Every AMD source module" in clean
    assert "amd build" not in _check_chips(base)


def test_amd_stale_by_git_dates(tmp_path):
    """A source committed after its build output flags stale; a build
    committed with or after its source does not; file-set-flagged
    modules stay out of the stale list (camp-tools#4 slice two)."""
    import subprocess
    from camp.checks import _amd_fileset, _amd_stale

    repo = tmp_path / "repo"
    (repo / "amd" / "src").mkdir(parents=True)
    (repo / "amd" / "build").mkdir()

    def git(*args, env_time=None):
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
               "PATH": "/usr/bin:/bin"}
        if env_time:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = env_time
        subprocess.run(["git", "-C", str(repo), *args],
                       check=True, capture_output=True, env=env)

    git("init", "-q")
    (repo / "amd" / "src" / "fresh.js").write_text("// v1")
    (repo / "amd" / "build" / "fresh.min.js").write_text("//v1")
    (repo / "amd" / "src" / "timediff.js").write_text("// v1")
    (repo / "amd" / "build" / "timediff.min.js").write_text("//v1")
    git("add", "-A")
    git("commit", "-qm", "initial build", env_time="2017-04-14T12:00:00")
    (repo / "amd" / "src" / "timediff.js").write_text("// v2 fix")
    git("add", "-A")
    git("commit", "-qm", "fix alignment", env_time="2022-08-04T12:00:00")

    fileset = _amd_fileset(repo)
    assert fileset["build_without_src"] == [] and fileset["src_without_build"] == []
    assert _amd_stale(repo, fileset) == ["timediff"]


def test_amd_stale_chip():
    from camp.site import _check_chips
    base = {"phplint": True, "errors": 0, "warnings": 0}
    html = _check_chips({**base, "amd": {"src": 2, "build": 2,
                                         "src_without_build": [],
                                         "build_without_src": [],
                                         "stale": ["timediff"]}})
    assert "1 stale" in html and "timediff" in html
    # orphans outrank stale
    both = _check_chips({**base, "amd": {"src": 2, "build": 3,
                                         "src_without_build": [],
                                         "build_without_src": ["mywords"],
                                         "stale": ["timediff"]}})
    assert "without source" in both and "1 stale" not in both


def _fake_rig(tmp_path):
    """A rig whose 'grunt' minifies by stripping comment lines — enough
    to exercise placement, rebuild, compare and cleanup."""
    rig = tmp_path / "rig"
    (rig / "lib").mkdir(parents=True)
    (rig / "lib" / "components.json").write_text(
        '{"plugintypes": {"mod": "mod", "local": "local"}}')
    bindir = rig / "node_modules" / ".bin"
    bindir.mkdir(parents=True)
    grunt = bindir / "grunt"
    grunt.write_text(
        "#!/bin/sh\n"
        "mkdir -p amd/build\n"
        "for f in amd/src/*.js; do\n"
        "  [ -e \"$f\" ] || exit 0\n"
        "  n=$(basename \"$f\" .js)\n"
        "  grep -v '^//' \"$f\" > \"amd/build/$n.min.js\"\n"
        "done\n")
    grunt.chmod(0o755)
    (rig / "mod").mkdir()
    (rig / "local").mkdir()
    return rig


def test_amd_rebuild_match_and_diff(tmp_path):
    """Byte-matching rebuilds certify; a committed output differing from
    the rebuild is named; unknown type paths record nothing
    (camp-tools#4 slice three)."""
    from camp.checks import _amd_rebuild
    rig = _fake_rig(tmp_path)

    repo = tmp_path / "plugin"
    (repo / "amd" / "src").mkdir(parents=True)
    (repo / "amd" / "build").mkdir()
    (repo / "amd" / "src" / "app.js").write_text("// comment\ncode();\n")
    (repo / "amd" / "build" / "app.min.js").write_text("code();\n")
    (repo / "amd" / "src" / "tampered.js").write_text("// c\nsafe();\n")
    (repo / "amd" / "build" / "tampered.min.js").write_text("evil();\n")

    verdict = _amd_rebuild(repo, rig, "mod_example")
    assert verdict == {"checked": 2, "differs": ["tampered"]}
    # the rig tree is left clean
    assert not (rig / "mod" / "example").exists()

    assert _amd_rebuild(repo, rig, "weirdtype_example") is None


def test_amd_rebuild_chip_states():
    from camp.site import _check_chips
    base = {"phplint": True, "errors": 0, "warnings": 0,
            "amd": {"src": 1, "build": 1, "src_without_build": [],
                    "build_without_src": [], "stale": []}}
    certified = dict(base, amd={**base["amd"], "rebuild": {"checked": 1, "differs": []}})
    html = _check_chips(certified)
    assert "rebuilt" in html and "byte for byte" in html
    differs = dict(base, amd={**base["amd"],
                              "rebuild": {"checked": 2, "differs": ["app"]}})
    html = _check_chips(differs)
    assert "rebuild differs (1)" in html and "app" in html
    # no rig verdict: the plain file-set check mark stands
    html = _check_chips(base)
    assert "rebuilt" not in html and "aria-hidden" in html
