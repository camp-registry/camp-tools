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
  repository's highest MOODLE_xxx_STABLE branch at or below it, else the
  newest release, else no ref (the repository's default branch);
- each resolved entry's own recorded dependencies and parent are
  followed the same way;
- an unlisted component is reported and skipped: the install will
  then fail exactly as it does today, and the report says why.
"""

from __future__ import annotations

import re
import subprocess
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


_STABLE_RE = re.compile(r"^MOODLE_(\d{3})_STABLE$")


def _code(branch: str) -> int:
    """'4.5' -> 405, the number inside MOODLE_405_STABLE."""
    major, minor = branch.split(".")
    return int(major) * 100 + int(minor)


def _stable_branches(source: str) -> list[str]:
    """MOODLE_xxx_STABLE branch names the repository has; [] on any failure."""
    try:
        out = subprocess.run(["git", "ls-remote", "--heads", source, "refs/heads/MOODLE_*_STABLE"],
                             capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    names = [line.rsplit("refs/heads/", 1)[-1] for line in out.stdout.splitlines() if "refs/heads/" in line]
    return [n for n in names if _STABLE_RE.match(n)]


def best_stable_branch(names: list[str], branch: str) -> str | None:
    """The highest MOODLE_xxx_STABLE at or below the branch under test: a
    repository maintaining 400 and 500 serves 4.5 from 400."""
    target = _code(branch)
    codes = sorted((int(m.group(1)), n) for n in names if (m := _STABLE_RE.match(n)))
    eligible = [n for c, n in codes if c <= target]
    return eligible[-1] if eligible else None


def _ref_for(entry: dict, branch: str, stable_branches) -> str | None:
    """Newest release covering the branch; else the repository's best
    MOODLE_xxx_STABLE branch for it (a plugin with no listed releases, or
    none covering the branch, usually maintains one per Moodle series);
    else the newest release; else the default branch."""
    releases = entry.get("releases") or []
    covering = [r for r in releases if branch in (r.get("supported-moodle") or [])]
    if covering:
        return str(covering[-1]["tag"])
    stable = best_stable_branch(stable_branches(entry["source"]), branch)
    if stable:
        return stable
    return str(releases[-1]["tag"]) if releases else None


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


def resolve(index_dir, version_text: str, moodle_branch: str,
            stable_branches=None) -> Resolution:
    """`stable_branches` lists a repository's MOODLE_xxx_STABLE heads; it
    defaults to a git ls-remote at call time (so tests can patch it)."""
    stable_branches = stable_branches or _stable_branches
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
                            ref=_ref_for(entry, branch, stable_branches), via=via))
        queue.extend((c, component) for c in _wanted(
            component, established, dict.fromkeys(_entry_dependencies(entry))))
    return res


def tsv_line(dep: Dep) -> str:
    return "\t".join([dep.component, dep.source, dep.ref or "", dep.via])
