"""Security advisories (RFC §5.3): validation, version matching, revocation.

Advisories live at index/advisories/<component>/<CAMP-YYYY-NNNN>.yml and are
authoritative index content. Downstream effects, all mechanical:

  - `camp composer` drops revoked versions from packages.json and emits
    every advisory in a Packagist-compatible security-advisories document,
    so `composer audit` warns automatically (RFC §6.1).
  - `camp site` shows advisories on the plugin page instead of the
    "no open advisories" card.
  - the release ledger is never touched: revocation removes a version from
    *installation channels*, the archive and history stay intact.

Version matching implements the subset of Composer constraint syntax the
schema allows: comma-separated AND of >=, <=, >, <, = against dotted
numeric versions (non-numeric suffixes are compared as strings, which is
fine for the plugin ecosystem's versioning in practice).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .validate import ValidationError  # noqa: F401  (re-exported for callers)

ADVISORIES_RELPATH = "advisories"
_OP_RE = re.compile(r"^(>=|<=|>|<|=)(.+)$")


def _version_key(version: str) -> tuple:
    """Sortable key: numeric dotted prefix, then any remaining suffix."""
    version = version.lstrip("vV")
    match = re.match(r"^(\d+(?:\.\d+)*)(.*)$", version)
    if not match:
        return ((), version)
    numbers = tuple(int(part) for part in match.group(1).split("."))
    return (numbers, match.group(2))


def version_matches(version: str, constraint: str) -> bool:
    """True if `version` satisfies the AND of all comparator clauses."""
    key = _version_key(version)
    for clause in constraint.split(","):
        match = _OP_RE.match(clause.strip())
        if not match:
            raise ValueError(f"unparseable constraint clause: {clause!r}")
        op, bound = match.group(1), _version_key(match.group(2).strip())
        ok = {
            ">=": key >= bound, "<=": key <= bound,
            ">": key > bound, "<": key < bound, "=": key == bound,
        }[op]
        if not ok:
            return False
    return True


@dataclass
class AdvisorySet:
    """All advisories in an index tree, keyed by component."""
    by_component: dict[str, list[dict]] = field(default_factory=dict)

    @classmethod
    def load(cls, index_dir: str | Path) -> "AdvisorySet":
        advisories = cls()
        root = Path(index_dir) / ADVISORIES_RELPATH
        for path in sorted(root.glob("*/*.yml")) if root.exists() else []:
            with open(path) as f:
                advisory = yaml.safe_load(f)
            advisories.by_component.setdefault(advisory["component"], []).append(advisory)
        return advisories

    def for_component(self, component: str) -> list[dict]:
        return self.by_component.get(component, [])

    def is_revoked(self, component: str, version: str) -> bool:
        return any(
            advisory.get("revoke") and version_matches(version, advisory["affected-versions"])
            for advisory in self.for_component(component))

    def affecting(self, component: str, version: str) -> list[dict]:
        return [advisory for advisory in self.for_component(component)
                if version_matches(version, advisory["affected-versions"])]


def validate_advisory(path: str | Path, index_dir: str | Path | None = None) -> list[str]:
    """Schema validation plus registry cross-checks."""
    import json

    import jsonschema

    from .validate import SCHEMA_DIR

    problems: list[str] = []
    try:
        with open(path) as f:
            advisory = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        return [str(exc)]

    with open(SCHEMA_DIR / "advisory.schema.json") as f:
        schema = json.load(f)
    validator = jsonschema.Draft202012Validator(schema)
    for error in sorted(validator.iter_errors(advisory), key=str):
        location = "/".join(str(p) for p in error.absolute_path) or "(root)"
        problems.append(f"{location}: {error.message[:160]}")
    if problems:
        return problems

    expected = Path(ADVISORIES_RELPATH) / advisory["component"] / f"{advisory['id']}.yml"
    if Path(path).parts[-3:] != expected.parts:
        problems.append(f"file belongs at index/{expected}")

    try:
        version_matches("1.0.0", advisory["affected-versions"])
    except ValueError as exc:
        problems.append(str(exc))

    if index_dir is not None:
        component = advisory["component"]
        plugintype = component.partition("_")[0]
        entry_path = Path(index_dir) / "plugins" / plugintype / f"{component}.yml"
        if not entry_path.exists():
            problems.append(f"component {component} is not in the index")
    return problems


def next_id(index_dir: str | Path, year: int) -> str:
    """Next sequential advisory id for the given year."""
    root = Path(index_dir) / ADVISORIES_RELPATH
    prefix = f"CAMP-{year}-"
    highest = 0
    for path in root.glob(f"*/{prefix}*.yml") if root.exists() else []:
        try:
            highest = max(highest, int(path.stem.rsplit("-", 1)[1]))
        except ValueError:
            continue
    return f"{prefix}{highest + 1:04d}"
