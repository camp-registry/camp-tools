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
from pathlib import Path

from .validate import load_entry
from .verify import _clone


def chip(summary: dict) -> tuple[str, str]:
    """(text, color) for rendering a check summary."""
    if not summary.get("phplint", True):
        return ("parse errors", "#e05d44")
    errors, warnings = summary.get("errors", 0), summary.get("warnings", 0)
    if errors:
        return (f"{errors} errors · {warnings} warnings", "#fe7d37")
    if warnings:
        return (f"0 errors · {warnings} warnings", "#23854f")
    return ("clean", "#23854f")


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


def _phpcs_totals(root: Path) -> tuple[int, int] | None:
    result = subprocess.run(
        ["phpcs", "--standard=moodle", "--extensions=php",
         "--report=json", "-q", str(root)],
        capture_output=True, text=True)
    try:
        report = json.loads(result.stdout or "{}")
        totals = report["totals"]
        return int(totals["errors"]), int(totals["warnings"])
    except (ValueError, KeyError):
        return None


def for_version(doc: dict | None, version: str) -> dict | None:
    if not doc:
        return None
    return (doc.get("versions") or {}).get(version)


def run_checks(index_dir: str | Path, out_dir: str | Path, log=print) -> int:
    """Compute summaries for every non-revoked release of every entry.
    Existing per-version results with matching commits are kept (checks
    are commit-deterministic); only new or changed versions run."""
    if not (shutil.which("php") and shutil.which("phpcs")):
        log("checks: php/phpcs not on PATH; skipping (summaries unchanged)")
        return 0
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
        doc = load(out, component) or {"component": component, "versions": {}}
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
                                     "phplint": phplint,
                                     "errors": totals[0], "warnings": totals[1]}
                written += 1
                log(f"checks: {component}@{version}: "
                    f"{totals[0]} errors, {totals[1]} warnings")
        doc["checked"] = today
        (out / f"{component}.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
    return written
