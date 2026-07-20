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
