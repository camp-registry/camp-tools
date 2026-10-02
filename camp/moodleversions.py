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
    # 5.3: pre-release row (camp-tools#65). Code = the dev $version when the
    # row was added; replace with the branching-date code when
    # MOODLE_503_STABLE appears, and drop the PRERELEASE entry.
    (503, "5.3", 2026100200),
]

# Branches in BRANCHES that Moodle has not released yet, by maturity
# ("beta" or "rc"), admitted once upstream's main declares MATURITY_BETA
# (camp-tools#65): feature freeze, when Moodle asks plugin authors to test,
# and the window the old directory's early-bird reward recognised. Alpha is
# never admitted. Display decorates these ("5.3 (rc)"); tool_camp installs
# on such a site like any other because the name is the same.
PRERELEASE: dict[str, str] = {"5.3": "rc"}

# $maturity constants in core's version.php, lowest first.
MATURITIES = ["MATURITY_ALPHA", "MATURITY_BETA", "MATURITY_RC", "MATURITY_STABLE"]
ADMIT_FROM = "MATURITY_BETA"


def maturity(name: str) -> str | None:
    """"beta"/"rc" for a pre-release branch, None for a released one."""
    return PRERELEASE.get(name)


def display_name(name: str) -> str:
    """The branch as the site prints it: "5.2", or "5.3 (rc)" while 5.3 is
    a pre-release."""
    tag = PRERELEASE.get(name)
    return f"{name} ({tag})" if tag else name


def branches_from_supported(supported: list[int]) -> list[str] | None:
    """Expand a version.php [min, max] branch-code range, e.g. [311, 401]
    -> ["3.11", "4.0", "4.1"]. None if the range doesn't parse."""
    if len(supported) != 2:
        return None
    low, high = supported
    names = [name for code, name, _ in BRANCHES if low <= code <= high]
    return names or None


def parse_supported_range(raw: str | None) -> list[int] | None:
    """The [min, max] branch codes out of a raw $plugin->supported value
    such as "[500, 503]"; None when it does not carry two integers."""
    import re
    if not raw:
        return None
    codes = [int(n) for n in re.findall(r"\d+", raw)]
    return codes if len(codes) == 2 else None


def effective_supported(release: dict) -> list[str]:
    """The branches a release record supports against the CURRENT table.
    A record stores `supported-moodle` as the list expanded on the day it
    was published; when it also carries the declared `supported-range`
    (camp-tools#64), that range is re-expanded here so a branch added to
    the table later (a new Moodle release) lights up without re-ingest.
    Falls back to the stored list when the range does not expand."""
    rng = release.get("supported-range")
    if rng:
        expanded = branches_from_supported(list(rng))
        if expanded:
            return expanded
    return list(release.get("supported-moodle") or [])


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


def _branch_name(code: int) -> str:
    major, minor = divmod(code, 100) if code >= 100 else divmod(code, 10)
    return f"{major}.{minor}"


def fetch_main_version() -> dict | None:
    """$branch, $version and $maturity from upstream main's core version.php
    (public/version.php since 5.1), or None when unreachable."""
    import re
    import urllib.request
    for path in ("public/version.php", "version.php"):
        try:
            with urllib.request.urlopen(
                    f"https://raw.githubusercontent.com/moodle/moodle/main/{path}",
                    timeout=20) as resp:
                text = resp.read(65536).decode(errors="replace")
        except Exception:
            continue
        branch = re.search(r"^\$branch\s*=\s*'(\d+)'", text, re.M)
        version = re.search(r"^\$version\s*=\s*(\d{10})", text, re.M)
        mat = re.search(r"^\$maturity\s*=\s*(MATURITY_[A-Z]+)", text, re.M)
        if branch and version and mat:
            return {"branch": int(branch.group(1)), "version": int(version.group(1)),
                    "maturity": mat.group(1)}
    return None


def _first_code(code: int, fetch_first_code=None) -> int | None:
    """The branching-date version code of MOODLE_<code>_STABLE, read from
    its core version.php (public/ since 5.1); None when unreachable."""
    if fetch_first_code is not None:
        return fetch_first_code(code)
    import re
    import urllib.request
    for path in ("public/version.php", "version.php"):
        try:
            with urllib.request.urlopen(
                    "https://raw.githubusercontent.com/moodle/moodle/"
                    f"MOODLE_{code}_STABLE/{path}", timeout=20) as resp:
                text = resp.read(65536).decode(errors="replace")
        except Exception:
            continue
        m = re.search(r"^\$version\s*=\s*(\d{8})", text, re.M)
        if m:
            return int(m.group(1)) * 100
    return None


def check_upstream(ls_remote: str | None = None,
                   fetch_first_code=None, main_version=None) -> list[dict]:
    """Compare BRANCHES against Moodle upstream.

    Findings, each with a ready-made table row (run weekly by CI; a finding
    means a human edits the table and ships):
    - an unknown stable branch (newer than our floor): add the row;
    - a known PRERELEASE branch whose stable branch now exists: promote it
      (fix the first-version code, drop the PRERELEASE entry);
    - main declares an unknown branch at MATURITY_BETA or later: add it as
      a pre-release row (camp-tools#65). `main_version` is the dict
      fetch_main_version() returns; pass {} to skip that probe.
    """
    import re
    import subprocess

    if ls_remote is None:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "https://github.com/moodle/moodle"],
            capture_output=True, text=True, timeout=60)
        result.check_returncode()
        ls_remote = result.stdout
    if main_version is None:
        main_version = fetch_main_version() or {}

    known = {code for code, _, _ in BRANCHES}
    floor = min(known)
    findings = []
    stable_codes = {int(m.group(1)) for m in
                    re.finditer(r"refs/heads/MOODLE_(\d+)_STABLE", ls_remote)}
    for code, name, _ in BRANCHES:
        if name in PRERELEASE and code in stable_codes:
            first = _first_code(code, fetch_first_code)
            findings.append({
                "code": code, "name": name, "first": first, "kind": "promote",
                "row": (f"    ({code}, \"{name}\", {first or '<branching-date>00'}),  "
                        f"# and remove {name!r} from PRERELEASE"),
            })
    if main_version and main_version.get("branch") not in known:
        code = main_version["branch"]
        if (code > max(known) and
                MATURITIES.index(main_version["maturity"]) >= MATURITIES.index(ADMIT_FROM)):
            tag = "beta" if main_version["maturity"] == "MATURITY_BETA" else "rc"
            findings.append({
                "code": code, "name": _branch_name(code), "first": main_version["version"],
                "kind": "prerelease",
                "row": (f"    ({code}, \"{_branch_name(code)}\", {main_version['version']}),  "
                        f"# pre-release; PRERELEASE[\"{_branch_name(code)}\"] = \"{tag}\""),
            })
    for code in sorted(stable_codes):
        if code in known or code < floor:
            continue
        name = _branch_name(code)
        first = _first_code(code, fetch_first_code)
        findings.append({
            "code": code, "name": name, "first": first, "kind": "stable",
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
