"""Composer repository metadata generator (RFC §6.1).

Emits a static `packages.json` describing every installable release, so that
Composer-managed Moodle 5.2+ sites can:

    composer config repositories.camp composer https://<repo-domain>
    composer require <vendor>/moodle-<component>

Dist URLs point at camp's source-verified artifacts. Moodle-branch support
and review tier are carried as extra metadata. Security advisories go out
twice in Composer's advisory format: the whole feed as
security-advisories.json (what tool_camp matches locally) and per package
under p2/, which is where `composer audit` actually looks.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from . import plugintypes, standardplugins
from .advisory import AdvisorySet
from .moodleversions import branch_names, branches_known_at, next_branch
from .validate import load_entry

PLUGIN_TYPE_PREFIX = "moodle-"
INSTALLER_PACKAGE = "moodle/composer-installer"
# Per-package metadata directory (Composer v2 protocol), beside packages.json.
METADATA_DIR = "p2"

# composer/semver VersionParser::normalize, ported. Composer refuses any
# version string outside this grammar, so a release whose author-chosen
# version doesn't fit is either rewritten (below) or left out of
# packages.json; the release ledger is untouched either way.
_MODIFIER = r"[._-]?(?:(?:stable|beta|b|RC|alpha|a|patch|pl|p)(?:(?:[.-]?\d+)*)?)?(?:[.-]?dev)?"
_COMPOSER_VERSION = re.compile(
    r"^v?\d{1,5}(?:\.\d+){0,3}" + _MODIFIER + r"$"
    r"|^v?\d{4}(?:[.:-]?\d{2}){1,6}(?:[.:-]?\d{1,3}){0,2}" + _MODIFIER + r"$",
    re.IGNORECASE)
# The Moodle-community "-rN" scheme (v5.2-r1 = first release for 5.2) is
# not a Composer stability suffix; "patch" is the one with the same
# meaning and ordering (5.2-patch1 < 5.2-patch2, both above 5.2).
_RELEASE_SUFFIX = re.compile(r"[._-]r(\d+)$", re.IGNORECASE)


def composer_version(version: str) -> str | None:
    """Composer-parseable form of a release version, or None when there is
    no faithful rewrite. Build metadata (+...) is dropped as Composer does."""
    candidate = version.split("+", 1)[0]
    if _COMPOSER_VERSION.match(candidate):
        return candidate
    rewritten = _RELEASE_SUFFIX.sub(lambda m: f"-patch{m.group(1)}", candidate)
    if rewritten != candidate and _COMPOSER_VERSION.match(rewritten):
        return rewritten
    return None


_CONSTRAINT_VERSION = re.compile(r"v?\d[\w.+-]*", re.IGNORECASE)


def composer_constraint(constraint: str) -> str:
    """An advisory's affected-versions range with each version rewritten the
    way packages.json rewrites it, so `composer audit` matches what it
    installed. Operators and separators pass through untouched."""
    return _CONSTRAINT_VERSION.sub(
        lambda m: composer_version(m.group(0)) or m.group(0), constraint)


def _vendor(entry: dict) -> str:
    for maintainer in entry["maintainers"]:
        if "github" in maintainer:
            return maintainer["github"].lower()
    return "camp"


def _package_name(entry: dict) -> str:
    return f"{_vendor(entry)}/{PLUGIN_TYPE_PREFIX}{entry['component']}"


def _uid(name: str, version: str) -> int:
    digest = hashlib.sha256(f"{name}@{version}".encode()).hexdigest()
    return int(digest[:12], 16)  # 48 bits: a PHP int on every platform


def _composer_type(component: str) -> str:
    plugintype = component.partition("_")[0]
    return f"moodle-{plugintype}"


def package_definition(entry: dict, base_url: str,
                       advisories: AdvisorySet | None = None,
                       artifacts_base: str | None = None,
                       skipped: list[str] | None = None) -> tuple[str, dict]:
    """(package name, {version: definition}) for one index entry. Versions
    revoked by a security advisory (RFC §5.3) are omitted from installation
    metadata; the release ledger and archive are untouched. Versions Composer
    cannot parse are omitted too and reported via `skipped`."""
    component = entry["component"]
    name = _package_name(entry)
    versions: dict[str, dict] = {}

    for release in entry["releases"]:
        version = release["version"].split(" ")[0]
        if advisories is not None and advisories.is_revoked(component, version):
            continue
        pkg_version = composer_version(version)
        if pkg_version is None:
            if skipped is not None:
                skipped.append(f"{component} {version}")
            continue
        # Composer verifies dist.shasum with SHA-1 only, so the SHA-256 the
        # ledger holds cannot go there; it rides in extra.camp instead.
        versions[pkg_version] = {
            "name": name,
            "version": pkg_version,
            # Under the v2 protocol Composer keys every version by `uid`
            # (undefined key "uid" otherwise); derived from the name and
            # version so it is stable across publishes with no state.
            "uid": _uid(name, pkg_version),
            "type": _composer_type(component),
            "license": [entry.get("license", "GPL-3.0-or-later")],
            "dist": {
                "type": "zip",
                "url": (f"{artifacts_base or base_url + '/artifacts'}/"
                        f"{component}/{component}-{version}.zip"),
            },
            "source": {
                "type": "git",
                "url": entry["source"],
                "reference": release["commit"],
            },
            "require": {
                INSTALLER_PACKAGE: "*",
                "php": f">={release.get('php-min', '7.4')}",
            },
            # Branch compatibility as resolver-visible constraints. conflict
            # (not require): it only bites when moodle/moodle is actually in
            # the dependency graph — tree-only installs are untouched. The
            # upper bound exists only when the author deliberately stopped
            # short of a branch that already existed at publish time; camp
            # doesn't invent claims in either direction.
            **_core_conflict(release),
            "extra": {
                "camp": {
                    "component": component,
                    "version": version,
                    "zip-sha256": release["zip-sha256"],
                    "tier": entry["tier"],
                    "labels": entry["labels"],
                    "supported-moodle": release["supported-moodle"],
                    "moodle-version": release["moodle-version"],
                    "published": release["published"],
                    # $plugin->dependencies at the tag (camp-tools#20):
                    # component -> minimum $plugin->version, or "any"
                    "dependencies": dict(release.get("dependencies") or {}),
                },
            },
            "time": release["published"],
        }
        if entry.get("status") == "moved":
            # Composer's native abandoned-with-replacement signal: composer
            # warns "package is abandoned, use <moved-to> instead" without
            # any camp-specific tooling (RFC §6.3).
            versions[pkg_version]["abandoned"] = entry["moved-to"]
            versions[pkg_version]["extra"]["camp"]["moved-to"] = entry["moved-to"]

    return name, versions


def _core_conflict(release: dict) -> dict:
    supported = [b for b in release.get("supported-moodle") or []
                 if b in branch_names()]
    if not supported:
        return {}
    parts = [f"<{supported[0]}"]
    successor = next_branch(supported[-1])
    published = str(release.get("published", "")).replace("-", "")[:8]
    if successor and published.isdigit() and \
            successor in branches_known_at(int(published)):
        parts.append(f">={successor}")
    return {"conflict": {"moodle/moodle": " || ".join(parts)}}


def _bundled_on(component: str, branches: list[str]) -> bool:
    """Moodle ships the component on at least one branch the release
    supports, so it is core there and never a Composer requirement."""
    return bool(set(standardplugins.standard_branches(component)) & set(branches))


def link_requirements(packages: dict[str, dict], established: dict,
                      unlinked: list[str] | None = None) -> None:
    """Add `require` entries pointing at other camp packages (camp-tools#51):
    the parent package for a subplugin of a third-party parent, and every
    $plugin->dependencies entry that names a listed plugin. The constraint
    is the exact list of the dependency's published versions whose own
    $plugin->version meets the declared floor, so Composer refuses early
    what Moodle's upgrade would refuse later, and installs in dependency
    order (moodle/composer-installer#4). Dependencies Moodle bundles are
    skipped silently; ones camp does not serve, or serves only in versions
    below the floor, are skipped and reported in `unlinked`, since a
    requirement nothing can satisfy would only make the package
    uninstallable."""
    by_component = {}
    for name, versions in packages.items():
        any_version = next(iter(versions.values()))
        by_component[any_version["extra"]["camp"]["component"]] = name

    def satisfying(dep: str, floor) -> list[str]:
        versions = packages[by_component[dep]]
        return sorted(v for v, d in versions.items()
                      if floor == "any" or d["extra"]["camp"]["moodle-version"] >= floor)

    for name, versions in packages.items():
        for pkg_version, definition in versions.items():
            camp = definition["extra"]["camp"]
            component = camp["component"]
            wants: dict[str, int | str] = {}
            parent = plugintypes.parent(component.partition("_")[0], established)
            if parent:
                wants[parent] = "any"
            for dep, floor in camp["dependencies"].items():
                wants.setdefault(dep, floor)
            for dep, floor in wants.items():
                if dep == component or _bundled_on(dep, camp["supported-moodle"]):
                    continue
                if dep not in by_component:
                    if unlinked is not None:
                        unlinked.append(f"{component} {camp['version']}: depends on "
                                        f"{dep}, which camp does not serve as a package")
                    continue
                allowed = satisfying(dep, floor)
                if not allowed:
                    if unlinked is not None:
                        unlinked.append(f"{component} {camp['version']}: depends on "
                                        f"{dep} >= {floor}, above every version camp serves")
                    continue
                definition["require"][by_component[dep]] = " || ".join(allowed)


def generate(index_dir: str | Path, base_url: str,
             artifacts_base: str | None = None,
             skipped: list[str] | None = None,
             unlinked: list[str] | None = None,
             requirements: bool = True) -> dict:
    """Build the full packages.json document from an index tree.

    Packages ride inline (Composer's "partial packages": resolution never
    fetches anything else), and the document also declares the v2 protocol
    (`metadata-url` + `available-packages`) with
    `security-advisories.metadata`. Composer only consults a repository's
    advisories at all under the v2 protocol, and the static alternative to
    a POST api-url is per-package metadata: `composer audit` fetches
    /p2/<name>.json for each installed package this repository lists and
    reads its `security-advisories` (camp-tools#46)."""
    advisories = AdvisorySet.load(index_dir)
    packages: dict[str, dict] = {}
    for entry_path in sorted(Path(index_dir).glob("plugins/*/*.yml")):
        entry = load_entry(entry_path)
        # 'moved' listings stay installable (published versions remain
        # published); only 'delisted' drops out of installation metadata.
        if entry.get("status", "active") == "delisted" or entry["tier"] < 2:
            continue
        name, versions = package_definition(entry, base_url, advisories,
                                            artifacts_base=artifacts_base,
                                            skipped=skipped)
        if versions:
            packages[name] = versions
    if requirements:
        link_requirements(packages, plugintypes.load_established(index_dir), unlinked)
    return {
        "packages": packages,
        # Host-relative on purpose: a mirror serving the same tree answers
        # its own metadata requests (MIRRORING.md) instead of sending
        # clients back to the origin.
        "metadata-url": f"/{METADATA_DIR}/%package%.json",
        "available-packages": sorted(packages),
        "security-advisories": {"metadata": True},
    }


def package_metadata(document: dict, advisories_doc: dict) -> dict[str, dict]:
    """The per-package v2 metadata files, keyed by path relative to
    packages.json: {"p2/<vendor>/moodle-<component>.json": {...}}. Each
    carries the package's versions (as a list, the v2 shape) and its
    advisories, empty list included, so an audit never meets a 404."""
    files: dict[str, dict] = {}
    for name, versions in document["packages"].items():
        files[f"{METADATA_DIR}/{name}.json"] = {
            "packages": {name: list(versions.values())},
            "security-advisories": advisories_doc["advisories"].get(name, []),
        }
    return files


def generate_advisories(index_dir: str | Path, base_url: str) -> dict:
    """Security advisories in Packagist-compatible shape, so `composer
    audit` surfaces them for installed packages (RFC §6.1)."""
    advisories = AdvisorySet.load(index_dir)
    entries_by_component = {}
    for entry_path in sorted(Path(index_dir).glob("plugins/*/*.yml")):
        entry = load_entry(entry_path)
        entries_by_component[entry["component"]] = entry

    document: dict[str, list] = {}
    for component, component_advisories in sorted(advisories.by_component.items()):
        entry = entries_by_component.get(component)
        if entry is None:
            continue
        name = _package_name(entry)
        document[name] = [{
            "advisoryId": advisory["id"],
            "packageName": name,
            "title": advisory["title"],
            "severity": advisory["severity"],
            "affectedVersions": composer_constraint(advisory["affected-versions"]),
            "link": f"{base_url}/advisories/{advisory['id']}.html",
            "cve": advisory.get("cve"),
            "reportedAt": advisory["published"],
            "sources": [{"name": "camp", "remoteId": advisory["id"]}],
        } for advisory in component_advisories]
    return {"advisories": document}


def write(index_dir: str | Path, base_url: str, out_path: str | Path,
          artifacts_base: str | None = None, requirements: bool = True) -> int:
    skipped: list[str] = []
    unlinked: list[str] = []
    document = generate(index_dir, base_url, artifacts_base=artifacts_base,
                        skipped=skipped, unlinked=unlinked, requirements=requirements)
    for item in skipped:
        print(f"warning: {item}: version not expressible in Composer's "
              f"grammar; left out of packages.json", file=sys.stderr)
    for item in unlinked:
        print(f"note: {item}; no Composer requirement written", file=sys.stderr)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump(document, f, indent=2, sort_keys=True)
        f.write("\n")

    # The whole-feed file stays: tool_camp downloads it and matches locally
    # (RFC §5.3). Composer itself reads the per-package files below.
    advisories_doc = generate_advisories(index_dir, base_url)
    advisories_path = out.parent / "security-advisories.json"
    with open(advisories_path, "w") as f:
        json.dump(advisories_doc, f, indent=2, sort_keys=True)
        f.write("\n")

    for rel_path, metadata in package_metadata(document, advisories_doc).items():
        path = out.parent / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(metadata, f, indent=2, sort_keys=True)
            f.write("\n")
    return len(document["packages"])
