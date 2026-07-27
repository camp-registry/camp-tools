"""Directory cross-check (camp-tools#14): audit entry sources against the
old moodle.org directory's published VCS URLs.

The directory's pluglist is a historical snapshot: sometimes it names the
true canonical repository that seeding missed (copy-farm accounts won
first-come races), and sometimes camp is ahead of it (renamed repos,
successor organizations). So this tool CLASSIFIES, it never repoints:

  match            source equals the directory URL (after normalization)
  claimed-differs  tier 1+ entry differs — flag-only, the maintainer owns it
  same-owner       same repository owner: a rename/move, directory is stale
  owner-alias      owners look like the same party (org/user alias)
  directory-dead   the directory's repository no longer exists
  shared-history   both alive, different owners, and the camp holder's root
                   commit is reachable in the directory repo (or vice
                   versa): one derives from the other — likely repoint
                   material, but reviewed, not automatic
  independent      both alive, different owners, disjoint histories — the
                   review queue proper (squashed copies land here too)
  probe-failed     liveness or history probe inconclusive
  missing          directory component absent from camp — seeding worklist

Every acted-on change stays a reviewed registry act; this output is the
worklist, not the decision.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

import yaml

from .scan import USER_AGENT, _repo_host_path, _shares_history

VCS_HOSTS = ("github.com/", "gitlab.com/", "bitbucket.org/")


def _norm(url: str) -> str:
    u = (url or "").strip().lower().rstrip("/")
    u = re.sub(r"^https?://(www\.)?", "", u)
    return u.removesuffix(".git")


def _repo_url(url: str) -> str:
    """https URL trimmed to host/owner/repo (drops deep paths like
    bitbucket /src/... suffixes)."""
    parts = _norm(url).split("/")
    return "https://" + "/".join(parts[:3]) if len(parts) >= 3 else "https://" + _norm(url)


def _owner(url: str) -> str:
    parts = _norm(url).split("/")
    return parts[1] if len(parts) > 1 else ""


def _owner_alias(a: str, b: str) -> bool:
    """Conservative same-party heuristic for owner names that differ only
    by decoration (kelsoncm vs moodle-by-kelsoncm). Strips separators and
    the words 'moodle' and 'by', then checks containment either way."""
    def strip(owner: str) -> str:
        s = re.sub(r"[-_.]", "", owner.lower())
        return s.replace("moodle", "").replace("by", "")
    sa, sb = strip(a), strip(b)
    if len(sa) < 4 or len(sb) < 4:
        return False
    return sa in sb or sb in sa


def _repo_alive(url: str) -> bool | None:
    """True/False when git can answer, None on timeout or local failure."""
    try:
        probe = subprocess.run(
            ["git", "ls-remote", "--heads", _repo_url(url)],
            capture_output=True, timeout=45,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        return None
    return probe.returncode == 0


def load_pluglist(source: str) -> dict[str, str]:
    """component -> VCS URL map from a pluglist.php JSON document (local
    path or URL). Components without a usable VCS-host URL are dropped."""
    if source.startswith(("http://", "https://")):
        req = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.load(resp)
    else:
        data = json.load(open(source))
    out = {}
    for plugin in data.get("plugins", []):
        component, url = plugin.get("component"), plugin.get("source")
        if component and url and _norm(url).startswith(VCS_HOSTS):
            out[component] = url
    return out


def crosscheck(index_dir: str | Path, pluglist: dict[str, str],
               token: str | None = None, probe: bool = True,
               log=print) -> dict[str, list]:
    """Classify every directory component against the index. Returns
    {class: [(component, camp_source, directory_source), ...]} with
    'missing' rows carrying (component, "", directory_source)."""
    index = Path(index_dir)
    entries: dict[str, dict] = {}
    for path in (index / "plugins").glob("*/*.yml"):
        entry = yaml.safe_load(path.open()) or {}
        if entry.get("component"):
            entries[entry["component"]] = entry

    classes: dict[str, list] = {name: [] for name in (
        "match", "claimed-differs", "same-owner", "owner-alias",
        "directory-dead", "shared-history", "independent", "probe-failed",
        "missing")}

    for component, dir_url in sorted(pluglist.items()):
        entry = entries.get(component)
        if entry is None:
            classes["missing"].append((component, "", dir_url))
            continue
        camp_url = entry.get("source", "")
        row = (component, camp_url, dir_url)
        if _norm(camp_url) == _norm(dir_url):
            classes["match"].append(row)
            continue
        if entry.get("tier", 0) >= 1:
            classes["claimed-differs"].append(row)
            continue
        camp_owner, dir_owner = _owner(camp_url), _owner(dir_url)
        if camp_owner == dir_owner:
            classes["same-owner"].append(row)
            continue
        if _owner_alias(camp_owner, dir_owner):
            classes["owner-alias"].append(row)
            continue
        if not probe:
            classes["independent"].append(row)
            continue
        alive = _repo_alive(dir_url)
        if alive is None:
            classes["probe-failed"].append(row)
            continue
        if not alive:
            classes["directory-dead"].append(row)
            continue
        shared = _shares_history(_repo_url(camp_url), _repo_url(dir_url), token)
        if shared is None:
            shared = _shares_history(_repo_url(dir_url), _repo_url(camp_url), token)
        if shared is None:
            classes["probe-failed"].append(row)
        elif shared:
            classes["shared-history"].append(row)
        else:
            classes["independent"].append(row)
        log(f"  {component}: {'shared-history' if shared else 'independent'}")

    return classes


def write_reports(classes: dict[str, list], out_dir: str | Path) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in classes.items():
        if name == "match":
            continue  # counted, not listed: the point is the exceptions
        with open(out / f"{name}.tsv", "w") as fh:
            fh.writelines(f"{c}\t{camp}\t{d}\n" for c, camp, d in rows)
