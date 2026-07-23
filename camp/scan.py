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
# Frankenstyle plugin-type prefixes, used to build targeted name searches.
# A prefix search (e.g. "moodle-mod_ in:name") returns a corpus small enough
# to paginate near-fully, and sorting it by recent activity surfaces new and
# low-star plugins that a stars-sorted "moodle in:name" (27k+ results, hard-
# capped at 1000 by GitHub) never reaches.
FRANKENSTYLE_PREFIXES = [
    "mod", "local", "block", "theme", "tool", "qtype", "auth", "enrol",
    "format", "filter", "atto", "tiny", "report", "availability",
    "profilefield", "paygw", "assignsubmission", "assignfeedback",
    "gradingform", "customfield", "datafield", "editor", "message",
    "quiz", "quizaccess", "repository", "search", "webservice", "logstore",
    "antivirus", "cachestore", "contenttype", "fileconverter", "media",
    "mlbackend", "plagiarism", "portfolio", "qbehaviour", "coursereport",
]
# (query, sort) pairs. Topic queries by stars (popular first); frankenstyle
# name queries by recent push (new/low-star plugins first).
DEFAULT_QUERY_SPECS = (
    [("topic:moodle-plugin fork:false", "stars"),
     ("moodle in:name fork:false", "stars")]
    + [(f"moodle-{prefix}_ in:name fork:false", "updated")
       for prefix in FRANKENSTYLE_PREFIXES]
)
DEFAULT_QUERIES = [spec[0] for spec in DEFAULT_QUERY_SPECS]  # names only, back-compat
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
    pushed_at: str | None = None
    forks: int = 0
    open_issues: int = 0


@dataclass
class ScanResult:
    candidate: Candidate
    outcome: str  # written | exists | copy | name-collision | no-version-php | bad-license | skipped-known | fetch-error
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


# Outcomes the recheck window never reopens. 'opted-out' is a maintainer's
# standing request (RFC §4.4): re-evaluating it would re-list a repository
# whose owner asked to be removed. Keyed by repository, so a later rename
# of the repo escapes the marker; the rename detector doesn't track
# unlisted repos, and that residual risk is accepted.
PERMANENT_OUTCOMES = frozenset({"opted-out"})


def should_skip(ledger: dict, full_name: str, today: str,
                recheck_days: int = DEFAULT_RECHECK_DAYS) -> bool:
    """Skip repos already evaluated within the recheck window. 'written'
    entries are never skipped by the ledger (the index itself is the
    authority for those); PERMANENT_OUTCOMES are always skipped."""
    record = ledger.get(full_name)
    if record is None or record.get("outcome") == "written":
        return False
    if record.get("outcome") in PERMANENT_OUTCOMES:
        return True
    last = datetime.date.fromisoformat(record["last-checked"])
    age = (datetime.date.fromisoformat(today) - last).days
    return age < recheck_days


def record_outcome(ledger: dict, candidate: Candidate, outcome: str,
                   detail: str, today: str, component: str | None = None) -> None:
    previous = ledger.get(candidate.full_name, {})
    entry = {
        "outcome": outcome,
        "detail": detail,
        "first-seen": previous.get("first-seen", today),
        "last-checked": today,
    }
    # Collision-class outcomes carry the component explicitly so the
    # claim-time check in index CI can grep the ledger without parsing
    # detail strings.
    if component and outcome in ("copy", "name-collision"):
        entry["component"] = component
    ledger[candidate.full_name] = entry


def _request(url: str, token: str | None, retries: int = 3) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)
        except (urllib.error.URLError, TimeoutError, OSError):
            # Network blip (DNS, connection reset, SSL/read timeout): retry a
            # few times with linear backoff, then report status 0 rather than
            # crashing the whole sweep.
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    return 0, b"", {}


def _search(query: str, limit: int, token: str | None, log,
            sort: str = "stars") -> tuple[list[Candidate], int]:
    """Returns (candidates, total_count). total_count is GitHub's reported
    match count for the query — used to decide whether the 1000-result cap
    is being hit and date-window sharding is needed."""
    candidates: list[Candidate] = []
    total = 0
    page = 1
    while len(candidates) < limit:
        per_page = min(100, limit - len(candidates))
        url = ("https://api.github.com/search/repositories?"
               + urllib.parse.urlencode({
                   "q": query, "sort": sort, "order": "desc",
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
        payload = json.loads(body)
        total = payload.get("total_count", total)
        items = payload.get("items", [])
        if not items:
            break
        for repo in items:
            # Authenticated search returns private repos the token can
            # access; a public index must never list a non-public source.
            if repo.get("private") or repo.get("visibility", "public") != "public":
                continue
            candidates.append(Candidate(
                full_name=repo["full_name"],
                html_url=repo["html_url"],
                owner=repo["owner"]["login"],
                description=(repo.get("description") or "").strip(),
                license_spdx=(repo.get("license") or {}).get("spdx_id"),
                stars=repo.get("stargazers_count", 0),
                default_branch=repo.get("default_branch", "HEAD"),
                archived=repo.get("archived", False),
                pushed_at=repo.get("pushed_at"),
                forks=repo.get("forks_count", 0),
                open_issues=repo.get("open_issues_count", 0),
            ))
        page += 1
    return candidates[:limit], total


# GitHub caps search results at 1000 per query. When a query matches more,
# split its pushed-date range into windows each under the cap (recursive
# bisection), so every match is reachable by paginating the windows. Used
# for the largest frankenstyle prefixes (local_ ~2.2k, mod_/block_ ~1.5k).
SEARCH_RESULT_CAP = 1000
SHARD_TARGET = 900  # leave margin: counts drift between check and fetch
GITHUB_EPOCH = "2010-01-01"  # earliest plausible Moodle plugin on GitHub


def _date_windows(base_query: str, token: str | None, log,
                  target: int = SHARD_TARGET) -> list[str]:
    """Bisect base_query's pushed-date range until each window matches fewer
    than `target` repos; return the windowed query strings."""
    lo = datetime.date.fromisoformat(GITHUB_EPOCH)
    hi = datetime.date.today() + datetime.timedelta(days=1)
    windows: list[str] = []
    stack = [(lo, hi)]
    while stack:
        start, end = stack.pop()
        wq = f"{base_query} pushed:{start.isoformat()}..{end.isoformat()}"
        _, count = _search(wq, 1, token, log, sort="updated")
        if count < target or (end - start).days <= 1:
            if count > 0:
                windows.append(wq)
        else:
            mid = start + (end - start) // 2
            stack.append((start, mid))
            stack.append((mid, end))
    return sorted(windows)


def _fetch_component(candidate: Candidate, token: str | None,
                     log=lambda *_: None) -> tuple[str, str | None, str]:
    """Read version.php at the repo root and extract the component name.

    Returns (status, component, text): status is "ok", "missing" (file
    genuinely absent or unparseable — a recordable rejection), or "transient"
    (server error — must NOT be recorded as a rejection). Core rate-limit
    exhaustion (403/429 with a zero remaining-quota header) is NOT transient:
    the fetcher waits for the reset and retries, so a long sweep completes
    instead of mass-failing once quota runs out. The file text is returned so
    the caller can classify the license from the Moodle GPL header, which
    GitHub's detector misses when a plugin ships no LICENSE file (the common
    case). With a token, uses the authenticated contents API (5000/hr core
    quota); the anonymous raw host throttles hard.
    """
    # Branch names may contain characters urllib refuses to send raw (a
    # non-ASCII default branch crashes putrequest with UnicodeEncodeError),
    # so the ref is always percent-encoded.
    ref = urllib.parse.quote(candidate.default_branch, safe="")
    if token:
        url = (f"https://api.github.com/repos/{candidate.full_name}/contents/"
               f"version.php?ref={ref}")
        req_token = token
    else:
        url = (f"https://raw.githubusercontent.com/{candidate.full_name}/"
               f"{ref}/version.php")
        req_token = None
    headers_accept = {"Accept": "application/vnd.github.raw+json"} if token else {}
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, **headers_accept,
        **({"Authorization": f"Bearer {req_token}"} if req_token else {}),
    })
    net_errors = 0
    while True:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return ("missing", None, "")
            if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
                reset = int(exc.headers.get("X-RateLimit-Reset", "0"))
                wait = min(max(0, reset - int(time.time())) + 1, 3600)
                log(f"  core quota exhausted; waiting {wait}s for reset")
                time.sleep(wait)
                continue
            if exc.code == 429:  # secondary rate limit, no reset header
                wait = int(exc.headers.get("Retry-After", "30")) + 1
                log(f"  secondary rate limit; waiting {wait}s")
                time.sleep(wait)
                continue
            return ("transient", None, "")
        except (urllib.error.URLError, TimeoutError, OSError):
            # Network blip (DNS, reset, SSL/read timeout): retry a few times
            # before giving up as transient — never crash the sweep.
            net_errors += 1
            if net_errors >= 3:
                return ("transient", None, "")
            time.sleep(2 * net_errors)
    text = body.decode(errors="replace")
    match = COMPONENT_RE.search(text)
    return ("ok", match.group(1), text) if match else ("missing", None, text)


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


# --- component-name collisions -----------------------------------------------
# A candidate whose component is already listed is one of three things: the
# listed repository itself seen again ("exists"), a detached copy that shares
# git history with it ("copy"; GitHub-flagged forks never get here because
# the search queries exclude them), or a genuinely independent plugin that
# picked the same name ("name-collision", handled case by case at claim time
# per NAMESPACE.md in camp-docs). A copy whose history was squashed or
# re-initialized classifies as name-collision; that errs toward human
# attention, which is the safe direction.


def _normalize_repo_url(url: str) -> str:
    tail = url.strip().lower().split("://")[-1]
    return tail.rstrip("/").removesuffix(".git")


def _repo_host_path(url: str) -> tuple[str, str] | None:
    host, _, path = _normalize_repo_url(url).partition("/")
    if not path or "." not in host:
        return None
    return host, path


def _listing_source(index_dir: str | Path, component: str) -> str | None:
    path = (Path(index_dir) / "plugins" / component.partition("_")[0]
            / f"{component}.yml")
    if not path.exists():
        return None
    with open(path) as f:
        entry = yaml.safe_load(f) or {}
    return entry.get("source")


def _root_commit(host: str, path: str, token: str | None) -> str | None:
    """The oldest commit SHA reachable from the default branch, via the
    commits API's last page. None when it can't be determined."""
    if host == "github.com":
        url = f"https://api.github.com/repos/{path}/commits?per_page=1"
        status, body, headers = _request(url, token)
        if status != 200:
            return None
        link = headers.get("Link") or headers.get("link") or ""
        last = re.search(r'<([^>]+)>;\s*rel="last"', link)
        if last:
            status, body, _ = _request(last.group(1), token)
            if status != 200:
                return None
        commits = json.loads(body)
        return commits[0].get("sha") if commits else None
    if "gitlab" in host:
        encoded = urllib.parse.quote(path, safe="")
        url = f"https://{host}/api/v4/projects/{encoded}/repository/commits?per_page=1"
        status, body, headers = _gitlab_request(url, None)
        if status != 200:
            return None
        pages = headers.get("x-total-pages") or headers.get("X-Total-Pages")
        if pages and pages != "1":
            status, body, _ = _gitlab_request(f"{url}&page={pages}", None)
            if status != 200:
                return None
        commits = json.loads(body)
        return commits[0].get("id") if commits else None
    return None


def _has_commit(host: str, path: str, sha: str, token: str | None) -> bool | None:
    """Whether the repository contains the commit. None = undetermined."""
    if host == "github.com":
        status, _, _ = _request(
            f"https://api.github.com/repos/{path}/commits/{sha}", token)
    elif "gitlab" in host:
        encoded = urllib.parse.quote(path, safe="")
        status, _, _ = _gitlab_request(
            f"https://{host}/api/v4/projects/{encoded}/repository/commits/{sha}",
            None)
    else:
        return None
    if status == 200:
        return True
    if status in (404, 422):
        return False
    return None


def _shares_history(candidate_url: str, listed_url: str,
                    token: str | None) -> bool | None:
    """Whether the candidate's root commit is reachable in the listed
    repository. The root is the right commit to probe: a copy diverges at
    the tip but keeps its origin, whichever repository came first."""
    cand = _repo_host_path(candidate_url)
    listed = _repo_host_path(listed_url)
    if not cand or not listed:
        return None
    sha = _root_commit(*cand, token)
    if not sha:
        return None
    return _has_commit(listed[0], listed[1], sha, token)


def classify_existing(index_dir: str | Path, candidate_url: str,
                      component: str, gh_token: str | None) -> tuple[str, str]:
    """(outcome, detail) for a candidate whose component is already listed.
    gh_token is a GitHub token; GitLab endpoints are probed unauthenticated."""
    source = _listing_source(index_dir, component)
    if not source or _normalize_repo_url(source) == _normalize_repo_url(candidate_url):
        return ("exists",
                f"component {component} already registered (first-come, RFC §8)")
    shared = _shares_history(candidate_url, source, gh_token)
    if shared:
        return ("copy",
                f"shares git history with {source}, which holds component {component}")
    probe = "" if shared is False else "; history probe inconclusive"
    return ("name-collision",
            f"independent repository declaring {component}, held by "
            f"{source}{probe}; see NAMESPACE.md")


def _metrics_dict(*, updated: str | None, stars: int, forks: int,
                  open_issues: int, archived: bool, checked: str,
                  latest_release: dict | None = None) -> dict:
    """Ordered upstream-activity metrics block (schema `metrics`). `updated`
    is omitted when the platform gave no timestamp; `checked` is always set so
    consumers can judge freshness."""
    metrics: dict = {}
    if updated:
        metrics["updated"] = updated
    metrics["stars"] = stars
    metrics["forks"] = forks
    metrics["open-issues"] = open_issues
    metrics["archived"] = archived
    if latest_release:
        metrics["latest-release"] = latest_release
    metrics["checked"] = checked
    return metrics


def _fetch_latest_release(host: str, path: str, token: str | None) -> dict | None:
    """Upstream's newest formal release (tag + date), or None. Plugins that
    only tag without releases are skipped — tag-list ordering is not
    reliably chronological on either platform."""
    if host == "github.com":
        status, body, _ = _request(
            f"https://api.github.com/repos/{path}/releases/latest", token)
        if status != 200:
            return None
        rel = json.loads(body)
        tag = rel.get("tag_name")
        if not tag:
            return None
        out = {"tag": tag}
        if rel.get("published_at"):
            out["date"] = rel["published_at"]
        return out
    if "gitlab" in host:
        api = (f"https://{host}/api/v4/projects/"
               f"{urllib.parse.quote(path, safe='')}/releases?per_page=1")
        status, body, _ = _request(api, None)
        if status != 200:
            return None
        rels = json.loads(body)
        if not rels:
            return None
        out = {"tag": rels[0].get("tag_name", "")}
        if not out["tag"]:
            return None
        if rels[0].get("released_at"):
            out["date"] = rels[0]["released_at"]
        return out
    return None


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
    entry["metrics"] = _metrics_dict(
        updated=candidate.pushed_at, stars=candidate.stars, forks=candidate.forks,
        open_issues=candidate.open_issues, archived=candidate.archived, checked=today,
    )
    return entry


# --- metric enrichment (backfill/refresh) -----------------------------------
# Discovered entries carry no activity signals until this pass fetches them
# from the source platform. Metrics are advisory (they rank verification work),
# so a repo that has gone 404 or a transient error simply leaves the entry
# unenriched rather than failing the run.

def _fetch_metrics(source: str, token: str | None, checked: str,
                   log) -> tuple[str, dict | None, str | None]:
    """Fetch upstream metrics for one source repo. Returns
    (status, metrics, canonical) where status is 'ok' (metrics populated),
    'gone' (404 — repo removed), or 'error' (transient/unsupported host —
    retry later). `canonical` is the repo's canonical URL when it differs
    from `source` — GitHub 301s renamed repos forever, so without this
    check a migration is invisible (it hid logstore_xapi's move for
    months)."""
    parsed = urllib.parse.urlparse(source)
    host = parsed.netloc
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]

    if host == "github.com":
        url = f"https://api.github.com/repos/{path}"
        status, body, headers = _request(url, token)
        if status == 403 and headers.get("X-RateLimit-Remaining") == "0":
            wait = max(0, int(headers.get("X-RateLimit-Reset", "0")) - int(time.time())) + 1
            log(f"  rate-limited; sleeping {wait}s")
            time.sleep(wait)
            status, body, headers = _request(url, token)
        if status == 404:
            return "gone", None, None
        if status != 200:
            log(f"  {path}: GitHub HTTP {status}")
            return "error", None, None
        repo = json.loads(body)
        canonical = None
        full_name = repo.get("full_name") or ""
        if full_name and full_name.lower() != path.lower():
            canonical = f"https://github.com/{full_name}"
        return "ok", _metrics_dict(
            updated=repo.get("pushed_at"), stars=repo.get("stargazers_count", 0),
            forks=repo.get("forks_count", 0), open_issues=repo.get("open_issues_count", 0),
            archived=repo.get("archived", False), checked=checked,
            latest_release=_fetch_latest_release("github.com", path, token),
        ), canonical

    if "gitlab" in host:
        api = (f"{parsed.scheme}://{host}/api/v4/projects/"
               f"{urllib.parse.quote(path, safe='')}")
        status, body, _ = _request(api, None)
        if status == 404:
            return "gone", None, None
        if status != 200:
            log(f"  {path}: GitLab HTTP {status}")
            return "error", None, None
        proj = json.loads(body)
        return "ok", _metrics_dict(
            updated=proj.get("last_activity_at"), stars=proj.get("star_count", 0),
            forks=proj.get("forks_count", 0), open_issues=proj.get("open_issues_count", 0),
            archived=proj.get("archived", False), checked=checked,
            latest_release=_fetch_latest_release(host, path, None),
        ), None

    log(f"  {source}: unsupported host, skipped")
    return "error", None, None


_BADGE_LINE = re.compile(r"^\[?!\[")          # image or linked-image (badge) line
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")  # [text](url) -> text


def _summary_from_readme(text: str) -> str | None:
    """Best-effort one-line summary from a README: the first prose line,
    skipping the title, badges, images, raw HTML, rules, quotes and tables.
    Heuristic and plain-text only (markdown emphasis/links stripped)."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    in_fence = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        if line[0] in "#>|":                       # heading, quote, table row
            continue
        if line.startswith("<") or _BADGE_LINE.match(line):  # HTML / badge / image
            continue
        if set(line) <= set("-=*_ "):              # horizontal rule / setext underline
            continue
        line = _MD_LINK.sub(r"\1", line)           # unwrap links to their text
        line = re.sub(r"^[-*+]\s+", "", line)       # drop a leading list marker
        line = re.sub(r"[*_`]+", "", line).strip()  # strip emphasis / code ticks
        if len(line) >= 12:
            return line[:300]
    return None


def _fetch_readme_summary(source: str, token: str | None, log) -> str | None:
    """Fetch a repo's README and derive a one-line summary. GitHub-only for now
    (GitLab has no comparable single-call raw-README endpoint by path)."""
    parsed = urllib.parse.urlparse(source)
    if parsed.netloc != "github.com":
        return None
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    url = f"https://api.github.com/repos/{path}/readme"
    status, body, headers = _request(url, token)
    if status == 403 and headers.get("X-RateLimit-Remaining") == "0":
        wait = max(0, int(headers.get("X-RateLimit-Reset", "0")) - int(time.time())) + 1
        log(f"  rate-limited; sleeping {wait}s")
        time.sleep(wait)
        status, body, headers = _request(url, token)
    if status != 200:
        return None
    import base64
    try:
        content = json.loads(body).get("content", "")
        text = base64.b64decode(content).decode(errors="replace")
    except (ValueError, TypeError):
        return None
    return _summary_from_readme(text)


def enrich(index_dir: str | Path, token: str | None = None, limit: int | None = None,
           force: bool = False, readme: bool = True,
           stale_days: int | None = None, log=print) -> dict:
    """Backfill/refresh entries with upstream `metrics` and, for discovered
    (Tier 0) entries whose source repo set no description, a one-line
    `summary` derived from its README.

    Metrics are advisory activity signals and refresh at every tier —
    claiming a plugin shouldn't freeze its liveness data. Summary scraping
    stays Tier 0 only: from Tier 1 up the summary is on its way to being
    replaced by the author's own .camp/listing.yml, so enrich never
    overwrites what an author (or the registry) set.

    Resumable: an entry is skipped once it has metrics and a summary (or can't
    gain one), unless `force` is set — so an interrupted run resumes cleanly and
    the metrics-only entries from an earlier pass still get their README summary.
    `limit` caps the number of repos contacted (for sampling)."""
    token = token or os.environ.get("GITHUB_TOKEN")
    today = datetime.date.today().isoformat()
    paths = sorted((Path(index_dir) / "plugins").glob("*/*.yml"))
    stats = {"metrics": 0, "summary": 0, "skipped": 0, "gone": 0, "error": 0,
             "renamed": 0, "flagged-renames": 0}
    fetched = 0

    for path in paths:
        if limit is not None and fetched >= limit:
            break
        with open(path) as f:
            entry = yaml.safe_load(f)
        if entry.get("status", "active") == "delisted":
            continue

        metrics_checked = (entry.get("metrics") or {}).get("checked", "")
        is_stale = (stale_days is not None and
                    (not metrics_checked or metrics_checked <
                     (datetime.date.today()
                      - datetime.timedelta(days=stale_days)).isoformat()))
        needs_metrics = force or is_stale or not entry.get("metrics")
        # README summary is a fallback only when the repo gave no description,
        # only for GitHub sources (see _fetch_readme_summary), and only at
        # Tier 0 — never overwrite a claimed entry's summary.
        needs_summary = (readme and entry.get("tier", 0) == 0
                         and "github.com" in entry.get("source", "")
                         and (force or not (entry.get("summary") or "").strip()))
        if not needs_metrics and not needs_summary:
            stats["skipped"] += 1
            continue

        fetched += 1
        changed = False

        if needs_metrics:
            status, metrics, canonical = _fetch_metrics(
                entry["source"], token, today, log)
            if status == "ok":
                entry["metrics"] = metrics
                stats["metrics"] += 1
                changed = True
                if canonical:
                    # The repo moved; GitHub redirects the old name forever,
                    # so only this check ever notices. Scanner-owned entries
                    # are auto-canonicalized; claimed entries belong to their
                    # maintainer — record and flag, never rewrite.
                    if entry.get("tier", 0) == 0:
                        log(f"  renamed: {entry['source']} -> {canonical} "
                            "(tier 0, source updated)")
                        entry["source"] = canonical
                        stats["renamed"] += 1
                    else:
                        log(f"  RENAMED (tier {entry.get('tier')}): "
                            f"{entry['source']} -> {canonical} — flagged in "
                            "metrics; maintainer/registry should update source")
                        entry["metrics"]["renamed-to"] = canonical
                        stats["flagged-renames"] += 1
            elif status == "gone":
                stats["gone"] += 1
                log(f"  gone: {entry['source']}")
                continue          # repo unreachable — a README fetch would 404 too
            else:
                stats["error"] += 1
                continue

        if needs_summary:
            summary = _fetch_readme_summary(entry["source"], token, log)
            if summary:
                entry["summary"] = summary
                stats["summary"] += 1
                changed = True

        if changed:
            with open(path, "w") as f:
                yaml.safe_dump(entry, f, sort_keys=False, allow_unicode=True)
            if (stats["metrics"] + stats["summary"]) % 250 == 0:
                log(f"  … {stats['metrics']} metrics, {stats['summary']} summaries")

    log(f"enriched: {stats['metrics']} metrics, {stats['summary']} summaries; "
        f"skipped {stats['skipped']}, gone {stats['gone']}, errors {stats['error']}; "
        f"{stats['renamed']} renames fixed, {stats['flagged-renames']} flagged")
    return stats


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
            pushed_at=repo.get("pushed_at"),
            forks=repo.get("forks_count", 0),
            open_issues=repo.get("open_issues_count", 0),
        )

        if not _is_acceptable_license(spdx):
            record_outcome(ledger, candidate, "bad-license",
                           "license: NOASSERTION (text unclassified)" if spdx is None
                           else f"license: {spdx} (from text; not GPL-compatible)", today)
            results.append(ScanResult(candidate, "bad-license"))
            continue

        fetch_status, component, _version_text = _fetch_component(candidate, token, log)
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
            outcome, detail = classify_existing(index, candidate.html_url,
                                                component, token)
            record_outcome(ledger, candidate, outcome, detail, today,
                           component=component)
            results.append(ScanResult(candidate, outcome, component))
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


def check_collisions(index_dir: str | Path, token: str | None = None,
                     component: str | None = None, reclassify: bool = False,
                     include_copies: bool = False, dry_run: bool = False,
                     log=print) -> dict:
    """Report same-component ledger entries; with reclassify=True, also
    backfill legacy 'exists' entries that point at a repository other than
    the listing's source, splitting them into copy / name-collision via the
    shared-history probe. Entries whose probe is inconclusive on both hosts
    are left as 'exists' for a later run rather than guessed at."""
    token = token or os.environ.get("GITHUB_TOKEN")
    index = Path(index_dir)
    ledger = load_ledger(index)
    today = datetime.date.today().isoformat()
    stats = {"collisions": [], "copies": [], "reclassified": 0, "inconclusive": 0}
    legacy = re.compile(r"component (\S+) already registered")

    for full_name, entry in sorted(ledger.items()):
        outcome = entry.get("outcome")
        if reclassify and outcome == "exists":
            match = legacy.search(entry.get("detail", ""))
            if not match:
                continue
            comp = match.group(1)
            source = _listing_source(index, comp)
            if not source:
                continue
            listed = _repo_host_path(source)
            if listed and listed[1] == full_name.lower():
                continue  # the listed repository itself, re-seen
            # Ledger keys carry no host; nearly all are GitHub, so probe
            # there first and fall back to GitLab.com.
            shared = None
            for url in (f"https://github.com/{full_name}",
                        f"https://gitlab.com/{full_name}"):
                shared = _shares_history(url, source, token)
                if shared is not None:
                    break
            if shared is None:
                stats["inconclusive"] += 1
                log(f"  ? {full_name}: probe inconclusive, left as exists")
                continue
            if shared:
                outcome, detail = ("copy", f"shares git history with {source}, "
                                           f"which holds component {comp}")
            else:
                outcome, detail = ("name-collision",
                                   f"independent repository declaring {comp}, "
                                   f"held by {source}; see NAMESPACE.md")
            entry.update({"outcome": outcome, "detail": detail,
                          "component": comp, "last-checked": today})
            stats["reclassified"] += 1
        if component and entry.get("component") != component:
            continue
        if outcome == "name-collision":
            stats["collisions"].append((full_name, entry))
        elif outcome == "copy":
            stats["copies"].append((full_name, entry))

    for full_name, entry in stats["collisions"]:
        log(f"name-collision: {full_name}  [{entry.get('component', '?')}]  "
            f"{entry.get('detail', '')}")
    if include_copies:
        for full_name, entry in stats["copies"]:
            log(f"copy: {full_name}  [{entry.get('component', '?')}]  "
                f"{entry.get('detail', '')}")
    log(f"{len(stats['collisions'])} name-collision(s), "
        f"{len(stats['copies'])} cop(ies)"
        + (f"; {stats['reclassified']} reclassified, "
           f"{stats['inconclusive']} inconclusive" if reclassify else ""))
    if reclassify and stats["reclassified"] and not dry_run:
        save_ledger(index, ledger)
    return stats


def refresh_metrics(index_dir: str | Path, components: list[str],
                    token: str | None = None, log=print) -> list[str]:
    """Immediately re-fetch upstream metrics for the named entries, outside
    enrich's staleness window. The use case is a source repoint (a claim PR
    that changes `source`): until the rolling refresh reaches the entry, it
    wears the previous repository's activity data and health phrase, up to
    two weeks for the seeding cohort (camp-tools#9). Rename handling
    matches enrich: tier 0 sources auto-canonicalize, claimed entries get
    metrics.renamed-to flagged. Returns the components that failed."""
    token = token or os.environ.get("GITHUB_TOKEN")
    today = datetime.date.today().isoformat()
    failed: list[str] = []
    for component in components:
        path = (Path(index_dir) / "plugins" / component.partition("_")[0]
                / f"{component}.yml")
        if not path.exists():
            log(f"  ! {component}: no listing file")
            failed.append(component)
            continue
        with open(path) as f:
            entry = yaml.safe_load(f) or {}
        status, metrics, canonical = _fetch_metrics(
            entry["source"], token, today, log)
        if status != "ok":
            log(f"  ! {component}: metrics fetch failed ({status})")
            failed.append(component)
            continue
        entry["metrics"] = metrics
        if canonical:
            if entry.get("tier", 0) == 0:
                entry["source"] = canonical
            else:
                entry["metrics"]["renamed-to"] = canonical
        with open(path, "w") as f:
            yaml.safe_dump(entry, f, sort_keys=False, allow_unicode=True)
        log(f"  refreshed {component} from {entry['source']}")
    return failed


def opt_out(index_dir: str | Path, components: list[str], reason: str = "",
            log=print) -> list[str]:
    """Remove discovered listings at maintainer request (RFC §4.4) and
    record a permanent 'opted-out' ledger entry per source repository so
    discovery never re-lists it. Only unclaimed Tier 0 listings without
    releases qualify: claimed listings are the maintainer's own file (they
    edit or delist it by PR), and released listings are never deleted at
    all (the archive keeps published history; delisting is a status
    change). Returns the components that could NOT be removed."""
    index = Path(index_dir)
    today = datetime.date.today().isoformat()
    ledger = load_ledger(index)
    failed: list[str] = []

    for component in components:
        path = (index / "plugins" / component.partition("_")[0]
                / f"{component}.yml")
        if not path.exists():
            log(f"  ! {component}: no listing file")
            failed.append(component)
            continue
        with open(path) as f:
            entry = yaml.safe_load(f) or {}
        if entry.get("tier", 0) >= 1:
            log(f"  ! {component}: claimed (tier {entry['tier']}); the "
                f"maintainer edits or delists their own entry by PR")
            failed.append(component)
            continue
        if entry.get("releases"):
            log(f"  ! {component}: has released versions; published history "
                f"is never deleted (use status: delisted)")
            failed.append(component)
            continue
        source = entry.get("source", "")
        listed = _repo_host_path(source)
        if not listed:
            log(f"  ! {component}: unparseable source {source!r}")
            failed.append(component)
            continue
        repo_key = listed[1]
        previous = ledger.get(repo_key, {})
        detail = "listing removed at maintainer request"
        if reason:
            detail += f" ({reason})"
        ledger[repo_key] = {
            "outcome": "opted-out",
            "detail": detail,
            "component": component,
            "first-seen": previous.get("first-seen", today),
            "last-checked": today,
        }
        path.unlink()
        log(f"  - {component}  ({repo_key}, opted out)")

    save_ledger(index, ledger)
    return failed


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
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
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
            # Same rule as the GitHub scanner: tokens see private projects.
            if project.get("visibility", "public") != "public":
                continue
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
    encoded_ref = urllib.parse.quote(ref, safe="")
    url = (f"{GITLAB_API}/projects/{encoded_project}/repository/files/"
           f"{encoded_path}/raw?ref={encoded_ref}")
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
                # The history probe talks to the GitHub API when the listed
                # holder lives there; the GitLab token would be rejected.
                outcome, detail = classify_existing(
                    index, candidate.html_url, component,
                    os.environ.get("GITHUB_TOKEN"))
                record_outcome(ledger, candidate, outcome, detail, today,
                               component=component)
                results.append(ScanResult(candidate, outcome, component))
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
    # Explicit --query overrides use stars sort; the default set pairs each
    # query with the sort that best surfaces its long tail.
    specs = [(q, "stars") for q in queries] if queries else DEFAULT_QUERY_SPECS
    index = Path(index_dir)
    today = datetime.date.today().isoformat()
    ledger = load_ledger(index)

    seen_repos: set[str] = set()
    seen_components: set[str] = set()
    results: list[ScanResult] = []

    for query, sort in specs:
        log(f"searching: {query}  (by {sort})")
        candidates, total = _search(query, limit, token, log, sort=sort)
        # Beyond the 1000-result cap the plain search can't reach the older
        # tail; date-shard the query so every match is paginable. Only name
        # searches are sharded (topic queries are small and the --limit is a
        # deliberate cap on those).
        if total > SEARCH_RESULT_CAP and "in:name" in query:
            windows = _date_windows(query, token, log)
            log(f"  {total} matches > cap; sharding into {len(windows)} date windows")
            for window in windows:
                extra, _ = _search(window, SEARCH_RESULT_CAP, token, log, sort=sort)
                candidates.extend(extra)

        for candidate in candidates:
            if candidate.full_name in seen_repos:
                continue
            seen_repos.add(candidate.full_name)

            if should_skip(ledger, candidate.full_name, today, recheck_days):
                results.append(ScanResult(candidate, "skipped-known"))
                continue

            # Fetch version.php first: it yields both the component name and,
            # via its Moodle GPL header, the license. GitHub's detector
            # reports no license for the many plugins that ship the GPL grant
            # as a file header rather than a root LICENSE file, so a
            # license-first gate would wrongly reject them.
            fetch_status, component, version_text = _fetch_component(candidate, token, log)
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

            # License precedence: the GitHub API value, else the GPL header.
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

            if not _name_matches_component(candidate.full_name, component):
                record_outcome(ledger, candidate, "needs-review",
                               f"declares {component} but repo name does not correspond; "
                               f"human sign-off required before listing (RFC §8)", today)
                results.append(ScanResult(candidate, "needs-review", component))
                continue

            plugintype = component.partition("_")[0]
            out_path = index / "plugins" / plugintype / f"{component}.yml"
            if out_path.exists():
                outcome, detail = classify_existing(index, candidate.html_url,
                                                    component, token)
                record_outcome(ledger, candidate, outcome, detail, today,
                               component=component)
                results.append(ScanResult(candidate, outcome, component))
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
