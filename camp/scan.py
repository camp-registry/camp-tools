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


@dataclass
class ScanResult:
    candidate: Candidate
    outcome: str  # written | exists | no-version-php | no-component | bad-license
    component: str | None = None


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


def _fetch_component(candidate: Candidate, token: str | None) -> str | None:
    """Read version.php at the repo root and extract the component name."""
    url = (f"https://raw.githubusercontent.com/{candidate.full_name}/"
           f"{candidate.default_branch}/version.php")
    status, body, _ = _request(url, token=None)  # raw host: no auth needed
    if status != 200:
        return None
    match = COMPONENT_RE.search(body.decode(errors="replace"))
    return match.group(1) if match else None


def _is_gpl(spdx: str | None) -> bool:
    return bool(spdx) and (spdx.startswith(GPL_PREFIXES))


def _entry_for(candidate: Candidate, component: str, today: str) -> dict:
    entry: dict = {
        "component": component,
        "source": candidate.html_url,
        "maintainers": [{"github": candidate.owner}],
        "tier": 0,
        "status": "active",
        "discovered": today,
        "releases": [],
    }
    if candidate.description:
        entry["summary"] = candidate.description[:300]
    return entry


def scan(index_dir: str | Path, queries: list[str] | None = None, limit: int = 30,
         token: str | None = None, dry_run: bool = False, log=print) -> list[ScanResult]:
    """Run discovery and write Tier 0 entries into the index tree."""
    token = token or os.environ.get("GITHUB_TOKEN")
    queries = queries or DEFAULT_QUERIES
    index = Path(index_dir)
    today = datetime.date.today().isoformat()

    seen_repos: set[str] = set()
    seen_components: set[str] = set()
    results: list[ScanResult] = []

    for query in queries:
        log(f"searching: {query}")
        for candidate in _search(query, limit, token, log):
            if candidate.full_name in seen_repos:
                continue
            seen_repos.add(candidate.full_name)

            if not _is_gpl(candidate.license_spdx):
                results.append(ScanResult(candidate, "bad-license"))
                continue

            component = _fetch_component(candidate, token)
            if component is None:
                results.append(ScanResult(candidate, "no-version-php"))
                continue
            if component in seen_components:
                results.append(ScanResult(candidate, "exists", component))
                continue

            plugintype = component.partition("_")[0]
            out_path = index / "plugins" / plugintype / f"{component}.yml"
            if out_path.exists():
                results.append(ScanResult(candidate, "exists", component))
                seen_components.add(component)
                continue

            if not dry_run:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with open(out_path, "w") as f:
                    yaml.safe_dump(_entry_for(candidate, component, today), f,
                                   sort_keys=False, allow_unicode=True)
            seen_components.add(component)
            results.append(ScanResult(candidate, "written", component))
            log(f"  + {component}  ({candidate.full_name}, ★{candidate.stars}, {candidate.license_spdx})")

    return results
