"""Ecosystem seeding scanner (RFC §4.4, Tier 0 discovery).

Finds GPL-licensed Moodle plugins on GitHub and writes metadata-only Tier 0
index entries: name, repo, description, discovery date. No artifacts are
referenced or hosted; a discovered listing is a search result plus a "claim
this plugin" path for its author, removable on request, no questions asked.

Discovery angles (each candidate must pass ALL acceptance checks):
  - search queries over topics and naming conventions
    (default: topic:moodle-plugin, then "moodle in:name")
  - acceptance: not a fork, GPL-family license, and a version.php at the
    repo root whose $plugin->component (or legacy $module->component) is a
    valid frankenstyle name

Component names already present in the index are never overwritten —
registered names map to their canonical source (RFC §8).

GitHub rate limits: unauthenticated is 10 searches/min and works fine for
small runs; set GITHUB_TOKEN for large sweeps. Raw file fetches go through
raw.githubusercontent.com, which does not count against the API quota.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml

USER_AGENT = "camp-seeding-scanner/0.1 (community Moodle plugin repository)"
DEFAULT_QUERIES = [
    "topic:moodle-plugin fork:false",
    "moodle in:name fork:false",
]
GPL_PREFIXES = ("GPL-", "AGPL-", "LGPL-")
# GPL-compatible permissive licenses (FSF compatibility list). Listed at
# Tier 0 with the license surfaced as a badge — disclosure, not gatekeeping.
# Deliberately absent: CC-BY-SA (one-way compatible with GPLv3 only),
# NOASSERTION (unclassifiable — a candidate for content-based re-checking).
COMPATIBLE_LICENSES = {
    "MIT", "MIT-0", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "0BSD",
    "ISC", "Unlicense", "CC0-1.0", "WTFPL", "Zlib",
}
COMPONENT_RE = re.compile(
    r"\$(?:plugin|module)->component\s*=\s*['\"]([a-z][a-z0-9]*_[a-z][a-z0-9_]*)['\"]"
)


@dataclass
class Candidate:
    full_name: str
    html_url: str
    owner: str
    description: str
    license_spdx: str | None
    stars: int
    default_branch: str
    archived: bool
    platform: str = "github"


@dataclass
class ScanResult:
    candidate: Candidate
    outcome: str  # written | exists | no-version-php | bad-license | skipped-known | fetch-error
    component: str | None = None


# --- scan ledger -------------------------------------------------------------
# Every evaluated repository is recorded in a ledger committed alongside the
# index (index/discovery/scan-ledger.yml): outcome, human-readable detail,
# first-seen and last-checked dates. This makes rejections auditable ("why
# isn't X listed?"), lets re-scans skip recently-checked repos instead of
# re-fetching them, and gives outreach a list of nearly-eligible plugins
# (e.g. rejected only for a missing license). Entries are re-evaluated once
# they are older than the recheck window.

LEDGER_RELPATH = Path("discovery") / "scan-ledger.yml"
DEFAULT_RECHECK_DAYS = 30


def load_ledger(index_dir: str | Path) -> dict:
    path = Path(index_dir) / LEDGER_RELPATH
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data.get("repos", {})


def save_ledger(index_dir: str | Path, repos: dict) -> None:
    path = Path(index_dir) / LEDGER_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write("# Scan ledger: every repository the discovery scan has evaluated,\n"
                "# with the outcome and why. Maintained by `camp scan`; do not edit\n"
                "# release data here — this file never affects installed artifacts.\n")
        yaml.safe_dump({"repos": dict(sorted(repos.items()))}, f,
                       sort_keys=False, allow_unicode=True)


def should_skip(ledger: dict, full_name: str, today: str,
                recheck_days: int = DEFAULT_RECHECK_DAYS) -> bool:
    """Skip repos already evaluated within the recheck window. 'written'
    entries are never skipped by the ledger (the index itself is the
    authority for those)."""
    record = ledger.get(full_name)
    if record is None or record.get("outcome") == "written":
        return False
    last = datetime.date.fromisoformat(record["last-checked"])
    age = (datetime.date.fromisoformat(today) - last).days
    return age < recheck_days


def record_outcome(ledger: dict, candidate: Candidate, outcome: str,
                   detail: str, today: str) -> None:
    previous = ledger.get(candidate.full_name, {})
    ledger[candidate.full_name] = {
        "outcome": outcome,
        "detail": detail,
        "first-seen": previous.get("first-seen", today),
        "last-checked": today,
    }


def _request(url: str, token: str | None) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def _search(query: str, limit: int, token: str | None, log) -> list[Candidate]:
    candidates: list[Candidate] = []
    page = 1
    while len(candidates) < limit:
        per_page = min(100, limit - len(candidates))
        url = ("https://api.github.com/search/repositories?"
               + urllib.parse.urlencode({
                   "q": query, "sort": "stars", "order": "desc",
                   "per_page": per_page, "page": page,
               }))
        status, body, headers = _request(url, token)
        if status == 403 and headers.get("X-RateLimit-Remaining") == "0":
            wait = max(0, int(headers.get("X-RateLimit-Reset", "0")) - int(time.time())) + 1
            log(f"  rate-limited; sleeping {wait}s")
            time.sleep(wait)
            continue
        if status != 200:
            log(f"  search failed (HTTP {status}): {body[:200]!r}")
            break
        items = json.loads(body).get("items", [])
        if not items:
            break
        for repo in items:
            candidates.append(Candidate(
                full_name=repo["full_name"],
                html_url=repo["html_url"],
                owner=repo["owner"]["login"],
                description=(repo.get("description") or "").strip(),
                license_spdx=(repo.get("license") or {}).get("spdx_id"),
                stars=repo.get("stargazers_count", 0),
                default_branch=repo.get("default_branch", "HEAD"),
                archived=repo.get("archived", False),
            ))
        page += 1
    return candidates[:limit]


def _fetch_component(candidate: Candidate, token: str | None) -> tuple[str, str | None]:
    """Read version.php at the repo root and extract the component name.

    Returns (status, component): status is "ok", "missing" (file genuinely
    absent or unparseable — a recordable rejection), or "transient" (rate
    limit / server error — must NOT be recorded as a rejection). With a
    token, uses the authenticated contents API (5000/hr core quota); the
    anonymous raw host throttles bursts hard.
    """
    if token:
        url = (f"https://api.github.com/repos/{candidate.full_name}/contents/"
               f"version.php?ref={candidate.default_branch}")
        req_token = token
    else:
        url = (f"https://raw.githubusercontent.com/{candidate.full_name}/"
               f"{candidate.default_branch}/version.php")
        req_token = None
    headers_accept = {"Accept": "application/vnd.github.raw+json"} if token else {}
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, **headers_accept,
        **({"Authorization": f"Bearer {req_token}"} if req_token else {}),
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        return ("missing" if exc.code == 404 else "transient", None)
    except urllib.error.URLError:
        return ("transient", None)
    match = COMPONENT_RE.search(body.decode(errors="replace"))
    return ("ok", match.group(1)) if match else ("missing", None)


def _is_gpl(spdx: str | None) -> bool:
    return bool(spdx) and (spdx.startswith(GPL_PREFIXES))


def _is_acceptable_license(spdx: str | None) -> bool:
    return _is_gpl(spdx) or (spdx in COMPATIBLE_LICENSES)


# Distinctive phrases from canonical license texts, for repos whose license
# file GitHub cannot fingerprint (NOASSERTION) — usually because of an added
# preamble, reflowed text, or a copyright header. Order matters: more
# specific variants (AGPL/LESSER, version numbers, BSD-3's extra clause)
# must precede the generic ones. First match wins.
LICENSE_TEXT_PATTERNS = [
    ("AGPL-3.0", ["gnu affero general public license"]),
    ("LGPL-3.0", ["gnu lesser general public license", "version 3"]),
    ("LGPL-2.1", ["gnu lesser general public license", "version 2.1"]),
    ("GPL-3.0", ["gnu general public license", "version 3"]),
    ("GPL-2.0", ["gnu general public license", "version 2"]),
    ("Apache-2.0", ["apache license", "version 2.0"]),
    ("MIT", ["permission is hereby granted, free of charge"]),
    ("BSD-3-Clause", ["redistribution and use in source and binary forms",
                      "neither the name"]),
    ("BSD-2-Clause", ["redistribution and use in source and binary forms"]),
]


def classify_license_text(text: str) -> str | None:
    """Best-effort SPDX id from a license file's text, None if unrecognized."""
    normalized = " ".join(text.lower().split())
    for spdx, phrases in LICENSE_TEXT_PATTERNS:
        if all(phrase in normalized for phrase in phrases):
            return spdx
    return None


def _name_matches_component(full_name: str, component: str) -> bool:
    """Weak-canonicality check (RFC §8): auto-listing requires the repository
    name to plausibly correspond to the component it declares. Repos that
    fail (e.g. a repo named WORDPRESS-02-x declaring mod_y) are recorded as
    needs-review for human sign-off rather than silently claiming the name."""
    repo_name = re.sub(r"[-_.]", "", full_name.split("/", 1)[1].lower())
    short_name = re.sub(r"[-_.]", "", component.partition("_")[2])
    full = re.sub(r"[-_.]", "", component)
    return short_name in repo_name or full in repo_name


def _entry_for(candidate: Candidate, component: str, today: str) -> dict:
    maintainer = {("gitlab" if candidate.platform == "gitlab" else "github"): candidate.owner}
    entry: dict = {
        "component": component,
        "source": candidate.html_url,
        "maintainers": [maintainer],
        "tier": 0,
        "status": "active",
        "discovered": today,
        "releases": [],
    }
    if candidate.license_spdx:
        entry["license"] = candidate.license_spdx
    if candidate.description:
        entry["summary"] = candidate.description[:300]
    return entry


def recheck_noassertion(index_dir: str | Path, token: str | None = None,
                        dry_run: bool = False, log=print) -> list[ScanResult]:
    """Re-examine ledger rejections whose license GitHub couldn't classify
    (NOASSERTION): fetch the actual license file, pattern-match its text,
    and admit repos that turn out to be GPL-family or GPL-compatible."""
    token = token or os.environ.get("GITHUB_TOKEN")
    index = Path(index_dir)
    today = datetime.date.today().isoformat()
    ledger = load_ledger(index)
    results: list[ScanResult] = []

    targets = [name for name, record in ledger.items()
               if record["outcome"] == "bad-license" and "NOASSERTION" in record["detail"]]
    log(f"re-checking {len(targets)} NOASSERTION repositories by license text")

    for full_name in targets:
        status, body, _ = _request(f"https://api.github.com/repos/{full_name}/license", token)
        if status == 404:
            spdx = None
        elif status != 200:
            log(f"  ? {full_name}: license fetch HTTP {status}, skipped")
            continue
        else:
            import base64
            text = base64.b64decode(json.loads(body).get("content", "")).decode(errors="replace")
            spdx = classify_license_text(text)

        status, body, _ = _request(f"https://api.github.com/repos/{full_name}", token)
        if status != 200:
            log(f"  ? {full_name}: repo fetch HTTP {status}, skipped")
            continue
        repo = json.loads(body)
        candidate = Candidate(
            full_name=full_name, html_url=repo["html_url"], owner=repo["owner"]["login"],
            description=(repo.get("description") or "").strip(), license_spdx=spdx,
            stars=repo.get("stargazers_count", 0),
            default_branch=repo.get("default_branch", "HEAD"),
            archived=repo.get("archived", False),
        )

        if not _is_acceptable_license(spdx):
            record_outcome(ledger, candidate, "bad-license",
                           "license: NOASSERTION (text unclassified)" if spdx is None
                           else f"license: {spdx} (from text; not GPL-compatible)", today)
            results.append(ScanResult(candidate, "bad-license"))
            continue

        fetch_status, component = _fetch_component(candidate, token)
        if fetch_status == "transient":
            results.append(ScanResult(candidate, "fetch-error"))
            continue
        if fetch_status == "missing":
            record_outcome(ledger, candidate, "no-version-php",
                           f"license {spdx} recovered from text, but no parseable "
                           f"version.php at root of branch {candidate.default_branch}", today)
            results.append(ScanResult(candidate, "no-version-php"))
            continue

        if not _name_matches_component(candidate.full_name, component):
            record_outcome(ledger, candidate, "needs-review",
                           f"declares {component} but repo name does not correspond; "
                           f"human sign-off required before listing (RFC §8)", today)
            results.append(ScanResult(candidate, "needs-review", component))
            continue

        plugintype = component.partition("_")[0]
        out_path = index / "plugins" / plugintype / f"{component}.yml"
        if out_path.exists():
            record_outcome(ledger, candidate, "exists",
                           f"component {component} already registered (first-come, RFC §8)", today)
            results.append(ScanResult(candidate, "exists", component))
            continue

        if not dry_run:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w") as f:
                yaml.safe_dump(_entry_for(candidate, component, today), f,
                               sort_keys=False, allow_unicode=True)
        record_outcome(ledger, candidate, "written",
                       f"listed as {component}; license {spdx} classified from text", today)
        results.append(ScanResult(candidate, "written", component))
        log(f"  + {component}  ({full_name}, {spdx} from text)")

    if not dry_run:
        save_ledger(index, ledger)
    return results


# --- GitLab discovery --------------------------------------------------------
# GitLab.com is the second-largest home for Moodle plugins after GitHub.
# The project search API is usable unauthenticated (rate-limited); set
# GITLAB_TOKEN for headroom. License comes from the projects API with
# license=true; its lowercase key maps to our SPDX forms, and unrecognized
# licenses fall back to the same license-text classifier used for GitHub.

GITLAB_API = "https://gitlab.com/api/v4"
GITLAB_DEFAULT_TERMS = ["moodle-mod_", "moodle-local_", "moodle-block_",
                        "moodle-theme_", "moodle-tool_", "moodle-qtype_",
                        "moodle-auth_", "moodle-enrol_", "moodle-format_",
                        "moodle-filter_", "moodle-atto_", "moodle-report_"]
# GitLab license "key" (lowercase) -> our SPDX form.
GITLAB_LICENSE_MAP = {
    "mit": "MIT", "apache-2.0": "Apache-2.0", "gpl-3.0": "GPL-3.0",
    "gpl-2.0": "GPL-2.0", "agpl-3.0": "AGPL-3.0", "lgpl-3.0": "LGPL-3.0",
    "lgpl-2.1": "LGPL-2.1", "bsd-3-clause": "BSD-3-Clause",
    "bsd-2-clause": "BSD-2-Clause", "unlicense": "Unlicense", "0bsd": "0BSD",
    "isc": "ISC", "cc0-1.0": "CC0-1.0", "zlib": "Zlib",
}


def _gitlab_request(url: str, token: str | None) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        **({"PRIVATE-TOKEN": token} if token else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)
    except urllib.error.URLError:
        return 0, b"", {}


def _gitlab_search(term: str, limit: int, token: str | None, log) -> list[Candidate]:
    candidates: list[Candidate] = []
    page = 1
    while len(candidates) < limit:
        per_page = min(100, limit - len(candidates))
        url = (f"{GITLAB_API}/projects?" + urllib.parse.urlencode({
            "search": term, "order_by": "star_count", "sort": "desc",
            "per_page": per_page, "page": page, "archived": "false",
            "license": "true",
        }))
        status, body, headers = _gitlab_request(url, token)
        if status == 429:
            wait = int(headers.get("Retry-After", "30")) + 1
            log(f"  rate-limited; sleeping {wait}s")
            time.sleep(wait)
            continue
        if status != 200:
            log(f"  gitlab search failed (HTTP {status})")
            break
        projects = json.loads(body)
        if not projects:
            break
        for project in projects:
            license_key = (project.get("license") or {}).get("key")
            candidates.append(Candidate(
                full_name=project["path_with_namespace"],
                html_url=project["web_url"],
                owner=project["namespace"].get("full_path") or project["namespace"]["path"],
                description=(project.get("description") or "").strip(),
                license_spdx=GITLAB_LICENSE_MAP.get(license_key),
                stars=project.get("star_count", 0),
                default_branch=project.get("default_branch") or "HEAD",
                archived=project.get("archived", False),
                platform="gitlab",
            ))
        if len(projects) < per_page:
            break
        page += 1
        time.sleep(0.3)  # politeness between pages
    return candidates[:limit]


def _gitlab_fetch_file(project_path: str, path: str, ref: str,
                       token: str | None) -> tuple[str, bytes | None]:
    encoded_project = urllib.parse.quote(project_path, safe="")
    encoded_path = urllib.parse.quote(path, safe="")
    url = f"{GITLAB_API}/projects/{encoded_project}/repository/files/{encoded_path}/raw?ref={ref}"
    status, body, _ = _gitlab_request(url, token)
    if status == 200:
        return "ok", body
    if status == 404:
        return "missing", None
    return "transient", None


def _gitlab_component(candidate: Candidate, token: str | None) -> tuple[str, str | None, str]:
    """(status, component, version_php_text). The version.php text is returned
    so the caller can classify the license from its header — Moodle plugins
    conventionally carry the GPL grant as a per-file header comment rather
    than a LICENSE file, so GitLab's file-based detector usually finds none."""
    status, body = _gitlab_fetch_file(candidate.full_name, "version.php",
                                      candidate.default_branch, token)
    if status != "ok":
        return status, None, ""
    text = body.decode(errors="replace")
    match = COMPONENT_RE.search(text)
    return ("ok", match.group(1), text) if match else ("missing", None, text)


def scan_gitlab(index_dir: str | Path, terms: list[str] | None = None, limit: int = 50,
                token: str | None = None, dry_run: bool = False, log=print,
                recheck_days: int = DEFAULT_RECHECK_DAYS) -> list[ScanResult]:
    """Discover Moodle plugins on GitLab.com and write Tier 0 entries."""
    token = token or os.environ.get("GITLAB_TOKEN")
    terms = terms or GITLAB_DEFAULT_TERMS
    index = Path(index_dir)
    today = datetime.date.today().isoformat()
    ledger = load_ledger(index)

    seen_repos: set[str] = set()
    seen_components: set[str] = set()
    results: list[ScanResult] = []

    for term in terms:
        log(f"gitlab search: {term}")
        for candidate in _gitlab_search(term, limit, token, log):
            ledger_key = f"gitlab.com/{candidate.full_name}"
            if ledger_key in seen_repos:
                continue
            seen_repos.add(ledger_key)

            if should_skip(ledger, ledger_key, today, recheck_days):
                results.append(ScanResult(candidate, "skipped-known"))
                continue

            # Fetch version.php first: it yields the component name and, via
            # its Moodle GPL header, the license — which GitLab's file-based
            # detector usually misses (plugins rarely ship a LICENSE file).
            fetch_status, component, version_text = _gitlab_component(candidate, token)
            if fetch_status == "transient":
                results.append(ScanResult(candidate, "fetch-error"))
                continue
            if fetch_status == "missing":
                record_outcome(ledger, candidate, "no-version-php",
                               f"no parseable version.php at root of branch "
                               f"{candidate.default_branch}", today)
                results.append(ScanResult(candidate, "no-version-php"))
                continue

            # License precedence: GitLab API value, else the version.php header.
            spdx = candidate.license_spdx
            if not _is_acceptable_license(spdx):
                spdx = classify_license_text(version_text) or spdx
                candidate.license_spdx = spdx
            if not _is_acceptable_license(spdx):
                record_outcome(ledger, candidate, "bad-license",
                               f"license: {spdx or 'none detected'}", today)
                results.append(ScanResult(candidate, "bad-license"))
                continue
            if component in seen_components:
                record_outcome(ledger, candidate, "exists",
                               f"component {component} already indexed this run", today)
                results.append(ScanResult(candidate, "exists", component))
                continue

            plugintype = component.partition("_")[0]
            out_path = index / "plugins" / plugintype / f"{component}.yml"
            if out_path.exists():
                record_outcome(ledger, candidate, "exists",
                               f"component {component} already registered "
                               f"(first-come, RFC §8)", today)
                results.append(ScanResult(candidate, "exists", component))
                seen_components.add(component)
                continue
            if not _name_matches_component(candidate.full_name, component):
                record_outcome(ledger, candidate, "needs-review",
                               f"declares {component} but repo name does not correspond; "
                               f"human sign-off required before listing (RFC §8)", today)
                results.append(ScanResult(candidate, "needs-review", component))
                continue

            if not dry_run:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with open(out_path, "w") as f:
                    yaml.safe_dump(_entry_for(candidate, component, today), f,
                                   sort_keys=False, allow_unicode=True)
            record_outcome(ledger, candidate, "written", f"listed as {component}", today)
            seen_components.add(component)
            results.append(ScanResult(candidate, "written", component))
            log(f"  + {component}  ({candidate.full_name}, ★{candidate.stars}, {spdx})")

    if not dry_run:
        save_ledger(index, ledger)
    return results


def scan(index_dir: str | Path, queries: list[str] | None = None, limit: int = 30,
         token: str | None = None, dry_run: bool = False, log=print,
         recheck_days: int = DEFAULT_RECHECK_DAYS) -> list[ScanResult]:
    """Run discovery and write Tier 0 entries into the index tree."""
    token = token or os.environ.get("GITHUB_TOKEN")
    queries = queries or DEFAULT_QUERIES
    index = Path(index_dir)
    today = datetime.date.today().isoformat()
    ledger = load_ledger(index)

    seen_repos: set[str] = set()
    seen_components: set[str] = set()
    results: list[ScanResult] = []

    for query in queries:
        log(f"searching: {query}")
        for candidate in _search(query, limit, token, log):
            if candidate.full_name in seen_repos:
                continue
            seen_repos.add(candidate.full_name)

            if should_skip(ledger, candidate.full_name, today, recheck_days):
                results.append(ScanResult(candidate, "skipped-known"))
                continue

            if not _is_acceptable_license(candidate.license_spdx):
                record_outcome(ledger, candidate, "bad-license",
                               f"license: {candidate.license_spdx or 'none detected'}", today)
                results.append(ScanResult(candidate, "bad-license"))
                continue

            fetch_status, component = _fetch_component(candidate, token)
            if fetch_status == "transient":
                # Rate limit or server error: leave the ledger untouched so
                # the repo is re-evaluated on the next scan.
                results.append(ScanResult(candidate, "fetch-error"))
                continue
            if fetch_status == "missing":
                record_outcome(ledger, candidate, "no-version-php",
                               f"no parseable version.php at root of branch "
                               f"{candidate.default_branch}", today)
                results.append(ScanResult(candidate, "no-version-php"))
                continue
            if component in seen_components:
                record_outcome(ledger, candidate, "exists",
                               f"component {component} already indexed this run", today)
                results.append(ScanResult(candidate, "exists", component))
                continue

            if not _name_matches_component(candidate.full_name, component):
                record_outcome(ledger, candidate, "needs-review",
                               f"declares {component} but repo name does not correspond; "
                               f"human sign-off required before listing (RFC §8)", today)
                results.append(ScanResult(candidate, "needs-review", component))
                continue

            plugintype = component.partition("_")[0]
            out_path = index / "plugins" / plugintype / f"{component}.yml"
            if out_path.exists():
                record_outcome(ledger, candidate, "exists",
                               f"component {component} already registered "
                               f"(first-come, RFC §8)", today)
                results.append(ScanResult(candidate, "exists", component))
                seen_components.add(component)
                continue

            if not dry_run:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with open(out_path, "w") as f:
                    yaml.safe_dump(_entry_for(candidate, component, today), f,
                                   sort_keys=False, allow_unicode=True)
            record_outcome(ledger, candidate, "written", f"listed as {component}", today)
            seen_components.add(component)
            results.append(ScanResult(candidate, "written", component))
            log(f"  + {component}  ({candidate.full_name}, ★{candidate.stars}, {candidate.license_spdx})")

    if not dry_run:
        save_ledger(index, ledger)
    return results
