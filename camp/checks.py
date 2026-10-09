"""Static code-check summaries — the registry's "prechecker".

The release pipeline already runs these checks as gates (hard) and
signals (warn-only, D23); this module persists a per-plugin summary so
the site can show it and READMEs can badge it, instead of the results
living only in CI logs. Summaries are computed from the ledger's
recorded commit — the same code the verified artifact was built from.

Summary document (checks/<component>.json in the dist tree), keyed by
version so the site's version picker can show the right result:
  {"component", "checked",
   "versions": {"5.0.3": {"tag", "commit", "phplint", "errors", "warnings"},
                "4.4.0": {...}}}

Requires `php` and `phpcs` (with the moodle standard, e.g. moodlehq/
moodle-cs installed via composer) on PATH; entries are skipped with a
note when the tools are unavailable, never guessed.
"""

from __future__ import annotations

import datetime
import json
import posixpath
from xml.etree import ElementTree
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from .validate import load_entry
from .verify import _clone

# Check results are commit-deterministic, so summaries from the previous
# publish are reusable verbatim — unless the checking itself changed.
# Bump this when the tools or standard change; prior summaries with a
# different (or missing) value are recomputed.
# Checker versions per facet (camp-tools#79): a bump invalidates only the
# summaries of its own facet, so a phpcs rule change never redoes the AMD
# rebuilds and vice versa. Each per-version summary records the versions
# it was computed with under "facets"; summaries from before #79 carry
# only the document-level "checker" and are mapped by _facets_of.
CODE_VERSION = 5   # lint + phpcs. 5: skip thirdpartylibs.xml locations (#78)
AMD_VERSION = 4    # AMD file-set, staleness, rebuild-and-diff. 4: grunt rig (#4)
FACETS = {"code": CODE_VERSION, "amd": AMD_VERSION}
CHECKER_VERSION = CODE_VERSION   # document-level field, kept for readers
STORE_DIRNAME = "checks"         # committed store in the index (camp-index#532)

# Subplugin type prefixes are not in the rig's lib/components.json; the
# common families the verified corpus actually uses live here. A prefix
# in neither map skips the rebuild with a note, never guesses.
SUBPLUGIN_PATHS = {
    "quizaccess": "mod/quiz/accessrule", "quiz": "mod/quiz/report",
    "logstore": "admin/tool/log/store", "atto": "lib/editor/atto/plugins",
    "tiny": "lib/editor/tiny/plugins", "assignsubmission": "mod/assign/submission",
    "assignfeedback": "mod/assign/feedback", "datafield": "mod/data/field",
    "customcertelement": "mod/customcert/element",
}


def _fetch_prior(reuse: str, component: str) -> dict | None:
    """Prior summary from the previously published site (URL base or dir)."""
    try:
        if reuse.startswith("https://"):
            req = urllib.request.Request(
                f"{reuse}/{component}.json",
                headers={"User-Agent": "camp-checks-reuse"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                doc = json.loads(resp.read(2 * 1024 * 1024))
        else:
            doc = json.loads((Path(reuse) / f"{component}.json").read_text())
    except Exception:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("versions"), dict):
        return None
    return doc


def _facets_of(summary: dict, doc: dict) -> dict:
    """The facet versions a summary was computed with. Pre-#79 summaries
    have none of their own: the document's checker applies to the code
    facet, and the AMD facet is AMD_VERSION when that checker is at
    least 4 (the AMD rig landed with checker 4 and has not moved since)."""
    facets = summary.get("facets")
    if isinstance(facets, dict):
        return {k: int(v) for k, v in facets.items()}
    checker = int(doc.get("checker") or 0)
    return {"code": checker, "amd": min(checker, AMD_VERSION) if checker else 0}


def _materialise(doc: dict | None) -> dict | None:
    """Give every summary its own "facets" (from the document-level checker
    when it has none), so the document's checker field can be rewritten
    without losing what each summary was computed with."""
    if doc is None:
        return None
    for summary in (doc.get("versions") or {}).values():
        if isinstance(summary, dict) and "facets" not in summary:
            summary["facets"] = _facets_of(summary, doc)
    return doc


def stale_facets(summary: dict | None, doc: dict, commit: str) -> set[str]:
    """Which facets of a release's summary need computing: all of them
    when there is no summary or its commit no longer matches the ledger,
    else those whose version is behind."""
    if not summary or summary.get("commit") != commit:
        return set(FACETS)
    have = _facets_of(summary, doc)
    return {f for f, v in FACETS.items() if have.get(f) != v}


# Fixed-colour consumers (shields-style badge JSON) map semantic status to
# hex here; the site renders status as theme-aware CSS classes instead.
STATUS_COLORS = {"ok": "#23854f", "warn": "#fe7d37", "bad": "#e05d44"}


def chip(summary: dict) -> tuple[str, str]:
    """(text, status) for rendering a check summary; status is ok|warn|bad."""
    if not summary.get("phplint", True):
        return ("parse errors", "bad")
    errors, warnings = summary.get("errors", 0), summary.get("warnings", 0)
    if errors:
        return (f"{errors} errors · {warnings} warnings", "warn")
    if warnings:
        return (f"0 errors · {warnings} warnings", "ok")
    return ("clean", "ok")


def load(checks_dir: str | Path | None, component: str) -> dict | None:
    if not checks_dir:
        return None
    path = Path(checks_dir) / f"{component}.json"
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text())
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def thirdparty_locations(root: Path) -> list[str]:
    """Locations declared in the plugin's thirdpartylibs.xml, normalised
    (no leading ./ or trailing /), relative to the plugin root. Moodle's
    own tooling and moodle-plugin-ci skip these for code checks: a bundled
    library is not the author's code. Missing or unreadable
    manifest: nothing is skipped."""
    manifest = root / "thirdpartylibs.xml"
    if not manifest.is_file():
        return []
    try:
        tree = ElementTree.parse(manifest)
    except ElementTree.ParseError:
        return []
    found = []
    for library in tree.getroot().iter("library"):
        raw = (library.findtext("location") or "").strip()
        location = posixpath.normpath(raw.strip("/")) if raw else ""
        if location in ("", ".") or location.startswith("../"):
            continue
        found.append(location)
    return found


def _is_thirdparty(path: Path, root: Path, locations: list[str]) -> bool:
    rel = path.relative_to(root).as_posix()
    return any(rel == loc or rel.startswith(loc + "/") for loc in locations)


def _phplint(root: Path) -> bool:
    skip = thirdparty_locations(root)
    for f in root.rglob("*.php"):
        if _is_thirdparty(f, root, skip):
            continue
        result = subprocess.run(["php", "-l", str(f)], capture_output=True)
        if result.returncode != 0:
            return False
    return True


def _phpcs_totals(root: Path) -> dict | None:
    cmd = ["phpcs", "--standard=moodle", "--extensions=php", "--report=json", "-q"]
    skip = thirdparty_locations(root)
    if skip:
        # phpcs matches ignore patterns against the full path; a declared
        # directory covers everything beneath it, a declared file itself.
        cmd.append("--ignore=" + ",".join(f"{root}/{loc}" for loc in skip))
    result = subprocess.run(cmd + [str(root)], capture_output=True, text=True)
    try:
        report = json.loads(result.stdout or "{}")
        totals = report["totals"]
        rules: dict[str, int] = {}
        files_hit = 0
        for f in report.get("files", {}).values():
            if f.get("messages"):
                files_hit += 1
            for m in f.get("messages", []):
                src = m.get("source", "unknown")
                rules[src] = rules.get(src, 0) + 1
        top = dict(sorted(rules.items(), key=lambda kv: -kv[1])[:8])
        return {"errors": int(totals["errors"]),
                "warnings": int(totals["warnings"]),
                "files": files_hit, "rules": top}
    except (ValueError, KeyError):
        return None


def _amd_fileset(root: Path) -> dict | None:
    """Build-output completeness without a rebuild — the first slice of
    the freshness check (camp-tools#4): every module in amd/src should
    have its amd/build counterpart and vice versa. An orphan build file
    is minified code with no reviewable source in the tree (the
    mod_minilesson mywords case: arrived via a contributor merge, source
    never committed); a source without its build is a module Moodle will
    never load. None when the plugin has no AMD at all."""
    src_dir, build_dir = root / "amd" / "src", root / "amd" / "build"
    if not src_dir.is_dir() and not build_dir.is_dir():
        return None
    src = ({p.stem for p in src_dir.glob("*.js")}
           if src_dir.is_dir() else set())
    build = ({p.name[:-len(".min.js")] for p in build_dir.glob("*.min.js")}
             if build_dir.is_dir() else set())
    return {"src": len(src), "build": len(build),
            "src_without_build": sorted(src - build),
            "build_without_src": sorted(build - src)}


def _amd_stale(repo: Path, fileset: dict) -> list[str]:
    """Modules whose source was committed after their build output was
    last built — the second slice of the freshness check (camp-tools#4).
    Judged at the checked-out release commit from git history alone: the
    tool_moodlebox case (source fixed in 2022, shipped build from 2017,
    so installed sites never ran the fix) is exactly this shape. Modules
    already flagged by the file-set check are excluded; a comment-only
    source edit can flag a rebuild-clean module, which is why this
    displays warn-only."""
    def last_commit(path: str) -> int | None:
        result = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%ct",
             "HEAD", "--", path],
            capture_output=True, text=True)
        out = result.stdout.strip()
        return int(out) if out else None

    src_names = {p.stem for p in (repo / "amd" / "src").glob("*.js")} \
        if (repo / "amd" / "src").is_dir() else set()
    flagged = set(fileset.get("src_without_build", [])) \
        | set(fileset.get("build_without_src", []))
    stale = []
    for name in sorted(src_names - flagged):
        src_date = last_commit(f"amd/src/{name}.js")
        build_date = last_commit(f"amd/build/{name}.min.js")
        if src_date and build_date and src_date > build_date:
            stale.append(name)
    return stale


def _rig_type_path(rig: Path, prefix: str) -> str | None:
    """Where a component of this type lives in the rig's Moodle tree,
    from the rig's own lib/components.json plus the subplugin map."""
    try:
        components = json.loads((rig / "lib" / "components.json").read_text())
        path = (components.get("plugintypes") or {}).get(prefix)
    except (OSError, ValueError):
        return None
    return path or SUBPLUGIN_PATHS.get(prefix)


def _amd_rebuild(repo: Path, rig: Path, component: str) -> dict | None:
    """Rebuild amd/build from amd/src with the rig's grunt and diff
    against the committed outputs — the freshness check's third slice
    (camp-tools#4). The rebuild is ephemeral; only the verdict persists.

    The survey behind the design (all 31 AMD-carrying verified releases):
    123 of 156 files byte-identical even with a mismatched-era toolchain,
    and every difference explained as an author's own build pipeline or a
    self-built bundle — so one pinned rig suffices, a byte-identical
    result is certifiable, and a difference is evidence for a human, not
    an alarm. Returns None (no verdict recorded) when the rig cannot run
    this plugin: unknown type path, grunt failure, timeout. Never
    guesses. Bump CHECKER_VERSION when the rig's Moodle branch or node
    toolchain moves."""
    prefix, _, name = component.partition("_")
    type_path = _rig_type_path(rig, prefix)
    grunt = rig / "node_modules" / ".bin" / "grunt"
    if type_path is None or not grunt.exists():
        return None
    dest = rig / type_path / name
    committed = {p.name: p.read_bytes()
                 for p in (repo / "amd" / "build").glob("*.min.js")} \
        if (repo / "amd" / "build").is_dir() else {}
    try:
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(repo, dest, ignore=shutil.ignore_patterns(".git"))
        if (dest / "amd" / "build").is_dir():
            shutil.rmtree(dest / "amd" / "build")
        result = subprocess.run(
            [str(grunt), "amd", "--force"], cwd=str(dest),
            capture_output=True, timeout=600,
            env={**os.environ, "BROWSERSLIST_IGNORE_OLD_DATA": "1"})
        rebuilt_dir = dest / "amd" / "build"
        rebuilt = {p.name: p.read_bytes() for p in rebuilt_dir.glob("*.min.js")} \
            if rebuilt_dir.is_dir() else {}
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        shutil.rmtree(dest, ignore_errors=True)
    if not rebuilt:
        return None
    # set differences are the file-set slice's business; the rebuild
    # verdict covers the files both sides have
    common = sorted(set(committed) & set(rebuilt))
    differs = [f[:-len(".min.js")] for f in common if committed[f] != rebuilt[f]]
    return {"checked": len(common), "differs": differs}


def for_version(doc: dict | None, version: str) -> dict | None:
    if not doc:
        return None
    return (doc.get("versions") or {}).get(version)


def _compute(repo: Path, r: dict, component: str, needed: set[str],
             summary: dict | None, moodle_rig: str | Path | None, log) -> dict | None:
    """One release's summary with the `needed` facets (re)computed at the
    checked-out commit; other facets are carried over from `summary`.
    None when the code facet was needed and phpcs produced no report."""
    out = dict(summary or {})
    out.update({"tag": r["tag"], "commit": r["commit"]})
    facets = dict(_facets_of(summary, {}) if summary else {})
    if "code" in needed:
        totals = _phpcs_totals(repo)
        if totals is None:
            return None
        out.update({"phplint": _phplint(repo), **totals})
        facets["code"] = CODE_VERSION
    if "amd" in needed:
        out.pop("amd", None)
        amd = _amd_fileset(repo)
        if amd is not None:
            amd["stale"] = _amd_stale(repo, amd)
            if moodle_rig:
                rebuild = _amd_rebuild(repo, Path(moodle_rig), component)
                if rebuild is not None:
                    amd["rebuild"] = rebuild
                else:
                    log(f"checks: {component}@{r['tag']}: rebuild unavailable "
                        "(rig cannot run this plugin); no verdict recorded")
            out["amd"] = amd
        facets["amd"] = AMD_VERSION
    out["facets"] = {k: facets[k] for k in sorted(facets)}
    return out


def _version_key(release: dict) -> str:
    return release["version"].split(" ")[0].lstrip("v")


def _released_entries(index_dir: Path):
    from .advisory import AdvisorySet
    advisories = AdvisorySet.load(index_dir)
    for entry_path in sorted(index_dir.glob("plugins/*/*.yml")):
        entry = load_entry(entry_path)
        if not entry["releases"] or entry.get("status", "active") == "delisted":
            continue
        releases = [r for r in entry["releases"]
                    if not advisories.is_revoked(entry["component"], r["version"].split(" ")[0])]
        yield entry, releases


def _write_doc(path: Path, doc: dict) -> None:
    doc["checker"] = CHECKER_VERSION
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, sort_keys=True) + "\n")


def _compute_versions(entry: dict, doc: dict, work: list[tuple[str, dict, set[str]]],
                      moodle_rig, log) -> int:
    """Clone once, then (re)compute the listed (version, release, facets).
    Returns how many summaries were written into doc["versions"]."""
    component = entry["component"]
    versions = doc.setdefault("versions", {})
    done = 0
    with tempfile.TemporaryDirectory(prefix="camp-checks-") as tmp:
        repo = Path(tmp) / "src"
        try:
            _clone(entry["source"], str(repo))
        except Exception as exc:
            log(f"checks: {component}: {exc}")
            return 0
        for version, r, needed in work:
            try:
                subprocess.run(["git", "-C", str(repo), "checkout", "--quiet",
                                r["commit"]], check=True, capture_output=True)
            except Exception as exc:
                log(f"checks: {component}@{version}: {exc}")
                continue
            prior = versions.get(version)
            summary = _compute(repo, r, component, needed,
                               prior if prior and prior.get("commit") == r["commit"] else None,
                               moodle_rig, log)
            if summary is None:
                log(f"checks: {component}@{version}: no phpcs report; skipped")
                continue
            versions[version] = summary
            done += 1
            log(f"checks: {component}@{version}: {'+'.join(sorted(needed))}: "
                f"{summary.get('errors', '?')} errors, {summary.get('warnings', '?')} warnings")
    return done


def run_checks(index_dir: str | Path, out_dir: str | Path, log=print,
               reuse: str | None = None,
               moodle_rig: str | Path | None = None,
               store: str | Path | None = None) -> int:
    """Publish-time summaries for every released entry into `out_dir`.

    Sources, in order: the committed store (`<index>/checks/`, camp-index#532),
    what `out_dir` already holds, then `reuse` (the previously published
    site's /checks base URL, or a directory). Publish computes only a
    release that has no summary at all, so a new release's check line
    appears with the release; a summary whose facet is behind its version
    is served as it is and left to `refresh`, so a checker bump never
    runs the whole archive inside a publish."""
    index = Path(index_dir)
    have_tools = bool(shutil.which("php") and shutil.which("phpcs"))
    if not have_tools:
        log("checks: php/phpcs not on PATH; reusing prior summaries only")
    store_dir = Path(store) if store else index / STORE_DIRNAME
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    written = 0
    for entry, releases in _released_entries(index):
        component = entry["component"]
        doc = _materialise(load(store_dir, component) or load(out, component))
        fetched = None
        if doc is None and reuse:
            fetched = _materialise(_fetch_prior(reuse, component))
            doc = fetched
        if doc is None:
            doc = {"component": component, "versions": {}}
        versions = doc.setdefault("versions", {})
        missing = [(_version_key(r), r, set(FACETS)) for r in releases
                   if versions.get(_version_key(r), {}).get("commit") != r["commit"]]
        if missing and have_tools:
            done = _compute_versions(entry, doc, missing, moodle_rig, log)
            written += done
            if done:
                doc["checked"] = today
        elif missing:
            log(f"checks: {component}: {len(missing)} version(s) need tools; "
                "skipped (never guessed)")
        elif fetched is not None:
            log(f"checks: {component}: reused {len(versions)} summaries from prior publish")
        if versions:
            _write_doc(out / f"{component}.json", doc)
    return written


def refresh(index_dir: str | Path, log=print, budget: int | None = None,
            moodle_rig: str | Path | None = None, reuse: str | None = None) -> dict:
    """Fill and age the committed store (`<index>/checks/`, camp-index#532):
    compute summaries that are missing or whose facet is behind its
    version, up to `budget` releases this run, missing first and then the
    stalest documents first, so a checker bump drains over a few runs and
    every run keeps what it did. A store document the previous publish
    already has (`reuse`) is imported rather than recomputed. Documents
    for entries that no longer have releases are removed."""
    index = Path(index_dir)
    store_dir = index / STORE_DIRNAME
    have_tools = bool(shutil.which("php") and shutil.which("phpcs"))
    today = datetime.date.today().isoformat()
    stats = {"computed": 0, "imported": 0, "pending": 0, "removed": 0}
    docs: dict[str, tuple[dict, dict, list]] = {}
    worklist: list[tuple[int, str, str, str, dict, set[str]]] = []
    live = set()
    for entry, releases in _released_entries(index):
        component = entry["component"]
        live.add(component)
        doc = _materialise(load(store_dir, component))
        if doc is None and reuse:
            doc = _materialise(_fetch_prior(reuse, component))
            if doc is not None:
                stats["imported"] += 1
                _write_doc(store_dir / f"{component}.json", doc)
        if doc is None:
            doc = {"component": component, "versions": {}}
        docs[component] = (entry, doc, releases)
        versions = doc.get("versions", {})
        for r in releases:
            version = _version_key(r)
            needed = stale_facets(versions.get(version), doc, r["commit"])
            if needed:
                # missing summaries (rank 0) ahead of stale ones (rank 1);
                # within a rank, the document checked longest ago first
                rank = 0 if versions.get(version, {}).get("commit") != r["commit"] else 1
                worklist.append((rank, str(doc.get("checked") or ""), component,
                                 version, r, needed))
    for path in sorted(store_dir.glob("*.json")) if store_dir.is_dir() else []:
        if path.stem not in live:
            path.unlink()
            stats["removed"] += 1
    worklist.sort(key=lambda w: (w[0], w[1], w[2], w[3]))
    if budget is not None:
        chosen, worklist = worklist[:budget], worklist[budget:]
    else:
        chosen, worklist = worklist, []
    stats["pending"] = len(worklist)
    if chosen and not have_tools:
        log(f"checks: php/phpcs not on PATH; {len(chosen)} release(s) left pending")
        stats["pending"] += len(chosen)
        return stats
    by_component: dict[str, list] = {}
    for _, _, component, version, r, needed in chosen:
        by_component.setdefault(component, []).append((version, r, needed))
    for component, work in by_component.items():
        entry, doc, _ = docs[component]
        done = _compute_versions(entry, doc, work, moodle_rig, log)
        stats["computed"] += done
        stats["pending"] += len(work) - done
        if done:
            doc["checked"] = today
            _write_doc(store_dir / f"{component}.json", doc)
    log(f"checks-refresh: {stats['computed']} computed, {stats['imported']} imported, "
        f"{stats['removed']} removed, {stats['pending']} still pending")
    return stats
