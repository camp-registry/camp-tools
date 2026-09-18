"""Parked family reviews: re-check the gate (camp-tools#49).

A "New subplugin family detected" review parks when the parent plugin is
not listed or does not declare the type in its db/subplugins.json. The
index records each parked prefix in discovery/parked-families.yml with the
repository whose db/subplugins.json to watch; the review issue is closed
while parked. The monthly scan runs `camp parked-families-check` and
reopens the issue for every prefix whose gate has flipped. Establishing
or rejecting stays a human decision on the reopened issue; this module
never writes to the index.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml

PARKED_PATH = "discovery/parked-families.yml"
_RAW = "https://raw.githubusercontent.com"
REQUIRED = ("parent", "watch", "issue")


def _fetch(url: str) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "camp-tools"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def load_parked(index_dir) -> dict:
    """{prefix: record} from the parked file; missing file means nothing
    parked. Malformed records raise: the registry's own data must parse."""
    path = Path(index_dir) / PARKED_PATH
    if not path.exists():
        return {}
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a mapping of prefix -> record")
    for prefix, record in doc.items():
        if not isinstance(record, dict) or any(k not in record for k in REQUIRED):
            raise ValueError(f"{path}: parked family '{prefix}' needs a mapping "
                             f"with {', '.join(REQUIRED)}")
    return doc


def watched_repos(record: dict) -> list[str]:
    """The `watch` repository first, then any `also-watch` (string or list)."""
    extra = record.get("also-watch") or []
    if isinstance(extra, str):
        extra = [extra]
    return [record["watch"], *extra]


def declared_types(owner_repo: str, fetch=None) -> set[str]:
    """Subplugin type prefixes a repository declares on its default branch.
    404 means it declares none; any other failure raises so a transient
    outage never reads as "still parked"."""
    status, body = (fetch or _fetch)(f"{_RAW}/{owner_repo}/HEAD/db/subplugins.json")
    if status == 404:
        return set()
    if status != 200:
        raise RuntimeError(f"cannot fetch db/subplugins.json of {owner_repo} "
                           f"(HTTP {status})")
    doc = json.loads(body)
    # both spellings carry the same names; older plugins have only the
    # path-valued plugintypes map
    return set(doc.get("subplugintypes") or doc.get("plugintypes") or {})


def parent_listed(index_dir, component: str) -> bool:
    return (Path(index_dir) / "plugins" / component.partition("_")[0]
            / f"{component}.yml").exists()


@dataclass
class Status:
    prefix: str
    parent: str
    issue: str
    declared_by: list[str]   # watched repos that declare the prefix today
    listed: bool             # the parent has a listing

    @property
    def flipped(self) -> bool:
        return bool(self.declared_by) and self.listed

    @property
    def state(self) -> str:
        if self.flipped:
            return "flipped"
        if self.declared_by:
            return "declared-unlisted"
        return "parked"


def check(index_dir, fetch=None) -> list[Status]:
    """One Status per parked prefix, in file order. `fetch` defaults to the
    module's HTTP fetcher at call time (so tests can patch it)."""
    out = []
    for prefix, record in load_parked(index_dir).items():
        declared_by = [repo for repo in watched_repos(record)
                       if prefix in declared_types(repo, fetch=fetch)]
        out.append(Status(prefix=prefix, parent=record["parent"],
                          issue=record["issue"], declared_by=declared_by,
                          listed=parent_listed(index_dir, record["parent"])))
    return out


def tsv_line(status: Status) -> str:
    return "\t".join([status.prefix, status.parent, status.issue,
                      ",".join(status.declared_by), status.state])
