"""Parse $plugin->dependencies from version.php text (camp-tools#20).

Moodle plugins declare inter-plugin dependencies in version.php as an
associative array of frankenstyle component => minimum version int (or the
core constant ANY_VERSION, whose value is the string 'any'). Core enforces
the declaration at install time, so the data is accurate in a way optional
metadata never is.

Like the scalar-field extraction elsewhere in camp, this is a textual
approximation, not a PHP evaluation. Its failure direction is deliberate:
anything it cannot read with confidence (conditional definitions, computed
values, variables) is dropped, so the result errs toward "no dependency
recorded", never toward wrong data.
"""

from __future__ import annotations

import re

# The [...] or array(...) body of the assignment. Non-greedy up to the first
# closer followed by ';' — dependency arrays hold scalar values, so nested
# brackets do not occur in well-formed declarations; a pathological body
# simply fails to match pairs and parses as empty.
_DEPS_BLOCK_RE = re.compile(
    r"\$(?:plugin|module)->dependencies\s*=\s*(?:\[|array\s*\()(.*?)[\])]\s*;",
    re.DOTALL,
)
_PAIR_RE = re.compile(
    r"['\"]([a-z][a-z0-9]*_[a-z][a-z0-9_]*)['\"]\s*=>\s*([^,]+?)\s*(?:,|$)"
)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"(?://|#).*")


def parse_dependencies(text: str) -> dict[str, int | str]:
    """Component -> minimum version int, or 'any' for ANY_VERSION.

    Empty dict when version.php declares no dependencies or the declaration
    is not statically readable. Keys must be valid frankenstyle; values must
    be integer literals or ANY_VERSION/'any' — anything else drops the pair.
    """
    match = _DEPS_BLOCK_RE.search(text)
    if not match:
        return {}
    body = _BLOCK_COMMENT_RE.sub("", match.group(1))
    body = "\n".join(_LINE_COMMENT_RE.sub("", line) for line in body.splitlines())
    deps: dict[str, int | str] = {}
    for component, raw in _PAIR_RE.findall(body):
        value = raw.strip()
        if value.isdigit():
            deps[component] = int(value)
        elif value == "ANY_VERSION" or value.strip("'\"").lower() == "any":
            deps[component] = "any"
    return deps
