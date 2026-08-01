"""Moodle-standard component knowledge: which plugins ship with core, per
branch (camp-tools#25).

A dependency on a component bundled with Moodle is not an install
requirement on branches where it ships, and core-ness is per-branch data:
mod_chat and mod_survey are standard through 4.5 and deleted from 5.0,
where they continue as separately distributed plugins. The committed table
in standardplugins.json records, for every component that ever appears in
core's own lists across the tracked branches, the branches where it is
standard and the branches whose deleted list names it.

Upstream sources (per stable branch): lib/plugins.json from Moodle 4.4,
and the arrays behind standard_plugins_list() / is_deleted_standard_plugin()
in lib/classes/plugin_manager.php on older branches. Besides the branches
in moodleversions.BRANCHES (the ones plugins can declare support for), the
table reaches back through HISTORIC_BRANCHES (2.6-3.8, as far as the
parseable source exists) purely so a component core dropped long ago keeps
its anchor: theme_bootstrapbase renders "shipped with Moodle up to 3.6",
not a dateless "removed". Like the BRANCHES table, the JSON file is
committed and hand-verifiable; `camp check-standard-plugins` alerts when it
drifts from upstream and --write refreshes it (a reviewed registry act,
never automatic).
"""

from __future__ import annotations

import json
import re
import urllib.request
from functools import lru_cache
from pathlib import Path

from .moodleversions import BRANCHES

# (branch code, branch string) for frozen pre-BRANCHES history. These
# branches never change; they exist in the table only to anchor when a
# long-gone component last shipped with core.
HISTORIC_BRANCHES = [
    (26, "2.6"), (27, "2.7"), (28, "2.8"), (29, "2.9"),
    (30, "3.0"), (31, "3.1"), (32, "3.2"), (33, "3.3"), (34, "3.4"),
    (35, "3.5"), (36, "3.6"), (37, "3.7"), (38, "3.8"),
]

DATA_PATH = Path(__file__).parent / "standardplugins.json"
_RAW_BASE = "https://raw.githubusercontent.com/moodle/moodle"


def _branch_ref(code: int) -> str:
    return f"MOODLE_{code}_STABLE"


@lru_cache(maxsize=1)
def load() -> dict:
    with open(DATA_PATH) as f:
        return json.load(f)


def standard_branches(component: str) -> list[str]:
    return (load()["components"].get(component) or {}).get("standard", [])


def deleted_branches(component: str) -> list[str]:
    return (load()["components"].get(component) or {}).get("deleted", [])


def classify(component: str, supported: list[str] | None) -> tuple | None:
    """How a dependency on `component` relates to Moodle core, judged
    against the dependent's supported branches (or the newest tracked
    branch when the dependent declares no range, as Tier 0/1 entries do:
    "standard today" is the honest default).

    Returns None when core's lists have never named the component, else:
      ("standard",)                  bundled on every relevant branch
      ("standard-until", u, s)       bundled up to branch u, separate from s
      ("removed",)                   only ever in deleted lists (historic)
    """
    known = load()["components"].get(component)
    if not known:
        return None
    order = load()["branches"]          # historic + tracked, oldest first
    tracked = [name for _, name, _ in BRANCHES]
    relevant = [b for b in (supported or tracked[-1:]) if b in tracked]
    if not relevant:
        relevant = tracked[-1:]
    standard = set(known.get("standard", []))
    if standard >= set(relevant):
        return ("standard",)
    if standard:
        until = max(standard, key=order.index)
        since = order[order.index(until) + 1] if order.index(until) + 1 < len(order) else None
        return ("standard-until", until, since)
    return ("removed",)


# --- generation / drift check ------------------------------------------------

# 'type' => array('name', 'name', ...) pairs inside a PHP array literal.
_PHP_TYPE_RE = re.compile(r"'([a-z0-9_]+)'\s*=>\s*array\(([^()]*)\)", re.DOTALL)
_PHP_NAME_RE = re.compile(r"'([a-z0-9_]+)'")


def _php_slice(text: str, function: str) -> str:
    start = text.find(f"function {function}")
    if start == -1:
        return ""
    end = text.find("public static function", start + 10)
    return text[start:end if end != -1 else len(text)]


def _components(by_type: dict[str, list[str]]) -> set[str]:
    return {f"{ptype}_{name}" for ptype, names in by_type.items() for name in names}


def _fetch(url: str) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "camp-tools"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def fetch_branch(code: int, fetch=_fetch) -> tuple[set[str], set[str]]:
    """(standard, deleted) component sets for one branch, from
    lib/plugins.json when the branch has it, else the plugin_manager.php
    arrays. Raises on unreachable sources: a partial table must never be
    written silently."""
    ref = _branch_ref(code)
    status, body = fetch(f"{_RAW_BASE}/{ref}/lib/plugins.json")
    if status == 200:
        doc = json.loads(body)
        return (_components(doc.get("standard", {})),
                _components(doc.get("deleted", {})))
    status, body = fetch(f"{_RAW_BASE}/{ref}/lib/classes/plugin_manager.php")
    if status != 200:
        raise RuntimeError(f"cannot fetch standard-plugin sources for {ref}")
    standard = {ptype: _PHP_NAME_RE.findall(names) for ptype, names
                in _PHP_TYPE_RE.findall(_php_slice(body, "standard_plugins_list"))}
    deleted = {ptype: _PHP_NAME_RE.findall(names) for ptype, names
               in _PHP_TYPE_RE.findall(_php_slice(body, "is_deleted_standard_plugin"))}
    return _components(standard), _components(deleted)


def build_table(fetch=_fetch, log=lambda *_: None) -> dict:
    components: dict[str, dict[str, list[str]]] = {}
    branches = HISTORIC_BRANCHES + [(code, name) for code, name, _ in BRANCHES]
    for code, branch in branches:
        standard, deleted = fetch_branch(code, fetch=fetch)
        log(f"  {branch}: {len(standard)} standard, {len(deleted)} deleted")
        for name in standard:
            components.setdefault(name, {}).setdefault("standard", []).append(branch)
        for name in deleted:
            components.setdefault(name, {}).setdefault("deleted", []).append(branch)
    return {"branches": [name for _, name in branches],
            "components": {name: components[name] for name in sorted(components)}}


def write_table(table: dict) -> None:
    with open(DATA_PATH, "w") as f:
        json.dump(table, f, indent=1, sort_keys=False)
        f.write("\n")
    load.cache_clear()
