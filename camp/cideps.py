"""Dependencies a plugin needs installed beside it for moodle-plugin-ci.

The index PR verification job installs the plugin under test into a fresh
Moodle. A plugin whose version.php declares dependencies on other
third-party plugins, or a subplugin whose parent is itself a third-party
plugin, cannot even install alone: Moodle refuses the missing dependency
or the unknown plugin type, and every static check after it fails for a
reason that has nothing to do with the release. `camp ci-deps` resolves
that set from the index so the workflow can `moodle-plugin-ci add-plugin`
each one before install.

Resolution, transitive, from the plugin's own version.php:

- $plugin->dependencies, plus the parent component when the plugin's
  type is a subplugin type (core table or an established family);
- components Moodle bundles on the branch under test are skipped;
- a listed component resolves to its source repository and a ref: the
  newest release whose supported range covers the branch, else the
  newest release, else no ref (the repository's default branch);
- each resolved entry's own recorded dependencies and parent are
  followed the same way;
- an unlisted component is reported and skipped: the install will
  then fail exactly as it does today, and the report says why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import plugintypes, standardplugins, versionphp

COMPONENT_RE = re.compile(
    r"\$(?:plugin|module)->component\s*=\s*['\"]([a-z][a-z0-9]*_[a-z][a-z0-9_]*)['\"]"
)
_BRANCH_CODE_RE = re.compile(r"^MOODLE_(\d)(\d\d)_STABLE$")


def branch_label(value: str) -> str:
    """'MOODLE_405_STABLE' -> '4.5'; '4.5' passes through."""
    m = _BRANCH_CODE_RE.match(value)
    if m:
        return f"{m.group(1)}.{int(m.group(2))}"
    return value


@dataclass
class Dep:
    component: str
    source: str
    ref: str | None          # tag to check out; None = default branch
    via: str                 # the component that pulled this one in


@dataclass
class Resolution:
    deps: list[Dep] = field(default_factory=list)
    unlisted: list[tuple[str, str]] = field(default_factory=list)   # (component, via)
    bundled: list[str] = field(default_factory=list)


def _entry(index: Path, component: str) -> dict | None:
    path = index / "plugins" / component.partition("_")[0] / f"{component}.yml"
    if not path.exists():
        return None
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _ref_for(entry: dict, branch: str) -> str | None:
    releases = entry.get("releases") or []
    covering = [r for r in releases if branch in (r.get("supported-moodle") or [])]
    pick = (covering or releases)
    return str(pick[-1]["tag"]) if pick else None


def _entry_dependencies(entry: dict) -> list[str]:
    releases = entry.get("releases") or []
    for r in reversed(releases):
        if r.get("dependencies"):
            return list(r["dependencies"])
    return list(entry.get("dependencies") or {})


def _wanted(component: str, established: dict, deps: dict) -> list[str]:
    """Direct wants of one component: declared deps plus a third-party parent."""
    parent = plugintypes.parent(component.partition("_")[0], established)
    wants = [c for c in deps if c != parent]
    if parent:
        wants.insert(0, parent)
    return wants


def resolve(index_dir, version_text: str, moodle_branch: str) -> Resolution:
    index = Path(index_dir)
    branch = branch_label(moodle_branch)
    established = plugintypes.load_established(index)
    match = COMPONENT_RE.search(version_text)
    root = match.group(1) if match else "?"
    res = Resolution()
    seen = {root}
    queue = [(c, root) for c in _wanted(
        root, established, versionphp.parse_dependencies(version_text))]
    while queue:
        component, via = queue.pop(0)
        if component in seen:
            continue
        seen.add(component)
        if branch in standardplugins.standard_branches(component):
            res.bundled.append(component)
            continue
        entry = _entry(index, component)
        if entry is None:
            res.unlisted.append((component, via))
            continue
        res.deps.append(Dep(component=component, source=entry["source"],
                            ref=_ref_for(entry, branch), via=via))
        queue.extend((c, component) for c in _wanted(
            component, established, dict.fromkeys(_entry_dependencies(entry))))
    return res


def tsv_line(dep: Dep) -> str:
    return "\t".join([dep.component, dep.source, dep.ref or "", dep.via])
