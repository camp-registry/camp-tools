"""Moodle branch knowledge: mapping version.php facts to branch lists.

Two author-declared facts drive compatibility:

  $plugin->supported = [311, 401];   // explicit [min, max] branch range
  $plugin->requires  = 2023100900;   // minimum core version code

`supported` is authoritative when present: the branch-code range expands
against the known-branches list. Otherwise `requires` maps to the branch it
belongs to, and only that single branch is claimed — the plugin probably
works on later branches too, but the registry does not invent claims the
author didn't make (authors can widen it via --supported-moodle or a new
release).

BRANCHES must be extended as Moodle releases. All codes through 5.2 are
verified against upstream tags (July 2026; 5.1 = 20251006, 5.2 =
20260420). NB: core's own version.php moved to public/version.php in
Moodle 5.1 — irrelevant here (plugins keep their layout) but a trap for
anything that ever reads the core file.
"""

from __future__ import annotations

# (branch code as in $plugin->supported, branch string, first core version code)
BRANCHES = [
    (39, "3.9", 2020061500),
    (310, "3.10", 2020110900),
    (311, "3.11", 2021051700),
    (400, "4.0", 2022041900),
    (401, "4.1", 2022112800),
    (402, "4.2", 2023042400),
    (403, "4.3", 2023100900),
    (404, "4.4", 2024042200),
    (405, "4.5", 2024100700),
    (500, "5.0", 2025041400),
    (501, "5.1", 2025100600),
    (502, "5.2", 2026042000),
]


def branches_from_supported(supported: list[int]) -> list[str] | None:
    """Expand a version.php [min, max] branch-code range, e.g. [311, 401]
    -> ["3.11", "4.0", "4.1"]. None if the range doesn't parse."""
    if len(supported) != 2:
        return None
    low, high = supported
    names = [name for code, name, _ in BRANCHES if low <= code <= high]
    return names or None


def branch_from_requires(requires: int) -> str | None:
    """The branch a $plugin->requires core version code belongs to."""
    match = None
    for _, name, first in BRANCHES:
        if requires >= first:
            match = name
    return match


def branch_names() -> list[str]:
    """All known branch strings, oldest first — the single source of truth
    for anything that orders or filters by Moodle branch."""
    return [name for _, name, _ in BRANCHES]


def check_upstream(ls_remote: str | None = None,
                   fetch_first_code=None) -> list[dict]:
    """Compare BRANCHES against Moodle's actual stable branches upstream.

    Returns a finding per unknown branch (newer than our floor), each with
    a ready-made table row when the branching date is fetchable. Run
    weekly by CI; a finding means a human adds one BRANCHES row and ships.
    """
    import re
    import subprocess
    import urllib.request

    if ls_remote is None:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "https://github.com/moodle/moodle"],
            capture_output=True, text=True, timeout=60)
        result.check_returncode()
        ls_remote = result.stdout

    known = {code for code, _, _ in BRANCHES}
    floor = min(known)
    findings = []
    for match in re.finditer(r"refs/heads/MOODLE_(\d+)_STABLE", ls_remote):
        code = int(match.group(1))
        if code in known or code < floor:
            continue
        major, minor = divmod(code, 100) if code >= 100 else divmod(code, 10)
        name = f"{major}.{minor}"
        first = None
        if fetch_first_code is None:
            # core version.php moved to public/ in 5.1 — try both
            for path in ("public/version.php", "version.php"):
                try:
                    with urllib.request.urlopen(
                            "https://raw.githubusercontent.com/moodle/moodle/"
                            f"MOODLE_{match.group(1)}_STABLE/{path}",
                            timeout=20) as resp:
                        text = resp.read(65536).decode(errors="replace")
                except Exception:
                    continue
                m = re.search(r"^\$version\s*=\s*(\d{8})", text, re.M)
                if m:
                    first = int(m.group(1)) * 100
                    break
        else:
            first = fetch_first_code(code)
        findings.append({
            "code": code, "name": name, "first": first,
            "row": (f"    ({code}, \"{name}\", {first})," if first
                    else f"    ({code}, \"{name}\", <branching-date>00),"),
        })
    return sorted(findings, key=lambda f: f["code"])


def next_branch(name: str) -> str | None:
    """The branch after `name` in release order (4.5 -> 5.0), or None if
    `name` is the newest known branch."""
    names = branch_names()
    try:
        i = names.index(name)
    except ValueError:
        return None
    return names[i + 1] if i + 1 < len(names) else None


def branches_known_at(yyyymmdd: int) -> list[str]:
    """Branches that existed (had branched) on a given date — used to
    distinguish an author's deliberate exclusion from mere ignorance of
    branches that didn't exist yet."""
    return [name for _, name, first in BRANCHES if first // 100 <= yyyymmdd]
