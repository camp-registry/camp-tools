"""Component-name knowledge, aggregated (camp-tools#33).

"Is this name in use, or has it ever been" has one answer assembled from
every authority the registry holds: the index (including moved and
delisted listings), the scan ledger's opted-out and collision records,
the old directory's published names (which decide a name per
NAMESPACE.md even when nothing is listed today), the Moodle-standard
components table, and the plugin-type tables. `camp check-name` prints
the aggregation for operators; names_dataset() feeds the site's lookup
page and the removed-listings page (#28).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import directorymap, plugintypes, standardplugins
from .advisory import AdvisorySet
from .moodleversions import BRANCHES


def _entry(index: Path, component: str) -> dict | None:
    prefix = component.partition("_")[0]
    path = index / "plugins" / prefix / f"{component}.yml"
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text()) or {}


def removals(index: Path) -> dict[str, dict]:
    """component -> opted-out ledger record (repo key folded in)."""
    from .scan import load_ledger
    out = {}
    for repo, record in load_ledger(index).items():
        if record.get("outcome") == "opted-out" and record.get("component"):
            out[record["component"]] = {**record, "repo": repo}
    return out


def _core_phrase(component: str) -> str | None:
    standard = standardplugins.standard_branches(component)
    if not standard:
        return None
    tracked = [name for _, name, _ in BRANCHES]
    if tracked[-1] in standard:
        return "ships with current Moodle"
    order = standardplugins.load()["branches"]
    until = max(standard, key=order.index)
    return f"shipped with Moodle up to {until}"


def name_report(index_dir: str | Path, component: str) -> list[tuple[str, str]]:
    """(fact-kind, sentence) pairs for one component, most binding first.
    An empty list means the registry knows nothing about the name."""
    index = Path(index_dir)
    facts: list[tuple[str, str]] = []
    established = plugintypes.load_established(index)

    entry = _entry(index, component)
    if entry:
        status = entry.get("status", "active")
        tier = entry.get("tier", 0)
        if status == "delisted":
            facts.append(("delisted", f"previously listed, now delisted "
                          f"(published history retained); last source "
                          f"{entry.get('source', '?')}"))
        else:
            word = {"active": "listed", "moved": "listed (moved)"}.get(status, status)
            facts.append(("listed", f"{word}, tier {tier}, source "
                          f"{entry.get('source', '?')}"))

    removed = removals(index).get(component)
    if removed:
        facts.append(("removed", f"removed at the maintainer's request on "
                      f"{removed.get('last-checked', '?')} — "
                      f"{removed.get('detail', '')} (repo {removed['repo']}; "
                      f"discovery will not re-list it)"))

    core = _core_phrase(component)
    if core:
        facts.append(("core", core))

    directory = directorymap.directory_source(component)
    if directory:
        facts.append(("directory", f"the old moodle.org directory published "
                      f"{directory} under this name; per NAMESPACE.md that "
                      f"decides the name — the claim or repoint path applies"))

    advisories = AdvisorySet.load(index).for_component(component)
    if advisories:
        facts.append(("advisories", f"{len(advisories)} security "
                      f"advisor{'y' if len(advisories) == 1 else 'ies'} on record"))

    prefix = component.partition("_")[0]
    if prefix not in plugintypes.known_prefixes(established):
        facts.append(("unknown-type", f"'{prefix}' is not a known plugin "
                      f"type; a new subplugin family needs establishment "
                      f"review before members can list (camp-tools#16)"))
    return facts


def names_dataset(index_dir: str | Path) -> dict:
    """Compact per-name records for the site lookup: the union of every
    name any authority knows. Keys per record: t tier, st status (only
    when not active), rm removal date, dir 1 when the old directory
    published it, core phrase. The page derives sentences client-side."""
    index = Path(index_dir)
    names: dict[str, dict] = {}

    for path in sorted(index.glob("plugins/*/*.yml")):
        entry = yaml.safe_load(path.read_text()) or {}
        component = entry.get("component")
        if not component:
            continue
        record: dict = {"t": entry.get("tier", 0)}
        if entry.get("status", "active") != "active":
            record["st"] = entry["status"]
        names[component] = record

    for component, removed in removals(index).items():
        names.setdefault(component, {})["rm"] = removed.get("last-checked", "")

    for component in directorymap.load()["components"]:
        names.setdefault(component, {})["dir"] = 1

    for component in standardplugins.load()["components"]:
        phrase = _core_phrase(component)
        if phrase:
            names.setdefault(component, {})["core"] = phrase

    established = plugintypes.load_established(index)
    return {"names": names,
            "prefixes": sorted(plugintypes.known_prefixes(established))}
