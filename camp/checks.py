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
CHECKER_VERSION = 1


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
    if not isinstance(doc, dict) or doc.get("checker") != CHECKER_VERSION:
        return None
    return doc


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


def _phplint(root: Path) -> bool:
    for f in root.rglob("*.php"):
        result = subprocess.run(["php", "-l", str(f)], capture_output=True)
        if result.returncode != 0:
            return False
    return True


def _phpcs_totals(root: Path) -> dict | None:
    result = subprocess.run(
        ["phpcs", "--standard=moodle", "--extensions=php",
         "--report=json", "-q", str(root)],
        capture_output=True, text=True)
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


def for_version(doc: dict | None, version: str) -> dict | None:
    if not doc:
        return None
    return (doc.get("versions") or {}).get(version)


def run_checks(index_dir: str | Path, out_dir: str | Path, log=print,
               reuse: str | None = None) -> int:
    """Compute summaries for every non-revoked release of every entry.
    Existing per-version results with matching commits are kept (checks
    are commit-deterministic); only new or changed versions run. `reuse`
    seeds from the previously published site (its /checks base URL, or a
    directory) so a fresh CI runner only computes what's actually new."""
    have_tools = bool(shutil.which("php") and shutil.which("phpcs"))
    if not have_tools:
        log("checks: php/phpcs not on PATH; reusing prior summaries only")
    from .advisory import AdvisorySet
    advisories = AdvisorySet.load(index_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    written = 0
    for entry_path in sorted(Path(index_dir).glob("plugins/*/*.yml")):
        entry = load_entry(entry_path)
        if not entry["releases"] or entry.get("status", "active") == "delisted":
            continue
        component = entry["component"]
        doc = load(out, component)
        fetched = None
        if doc is None and reuse:
            fetched = _fetch_prior(reuse, component)
            doc = fetched
        if doc is None:
            doc = {"component": component, "versions": {}}
        doc["checker"] = CHECKER_VERSION
        versions = doc.setdefault("versions", {})
        pending = []
        for r in entry["releases"]:
            version = r["version"].split(" ")[0].lstrip("v")
            if advisories.is_revoked(component, r["version"].split(" ")[0]):
                continue
            if versions.get(version, {}).get("commit") == r["commit"]:
                continue
            pending.append((version, r))
        if not pending:
            if fetched is not None:
                # entirely reused from the previous publish: persist it so
                # the site (and the next publish) can read it from here
                (out / f"{component}.json").write_text(
                    json.dumps(doc, sort_keys=True) + "\n")
                log(f"checks: {component}: reused "
                    f"{len(versions)} summaries from prior publish")
            continue
        if not have_tools:
            log(f"checks: {component}: {len(pending)} version(s) need tools; "
                "skipped (never guessed)")
            if fetched is not None:
                (out / f"{component}.json").write_text(
                    json.dumps(doc, sort_keys=True) + "\n")
            continue
        with tempfile.TemporaryDirectory(prefix="camp-checks-") as tmp:
            repo = Path(tmp) / "src"
            try:
                _clone(entry["source"], str(repo))
            except Exception as exc:
                log(f"checks: {component}: {exc}")
                continue
            for version, r in pending:
                try:
                    subprocess.run(["git", "-C", str(repo), "checkout", "--quiet",
                                    r["commit"]], check=True, capture_output=True)
                except Exception as exc:
                    log(f"checks: {component}@{version}: {exc}")
                    continue
                phplint = _phplint(repo)
                totals = _phpcs_totals(repo)
                if totals is None:
                    log(f"checks: {component}@{version}: no phpcs report; skipped")
                    continue
                versions[version] = {"tag": r["tag"], "commit": r["commit"],
                                     "phplint": phplint, **totals}
                written += 1
                log(f"checks: {component}@{version}: "
                    f"{totals['errors']} errors, {totals['warnings']} warnings")
        doc["checked"] = today
        (out / f"{component}.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
    return written
