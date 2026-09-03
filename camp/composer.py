"""Composer repository metadata generator (RFC §6.1).

Emits a static `packages.json` describing every installable release, so that
Composer-managed Moodle 5.2+ sites can:

    composer config repositories.camp composer https://<repo-domain>
    composer require <vendor>/moodle-<component>

Dist URLs point at camp's source-verified artifacts. Moodle-branch support
and review tier are carried as extra metadata. Security advisories in
Composer's advisory format (for `composer audit`) belong to the advisory
pipeline and are not generated here yet.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .advisory import AdvisorySet
from .moodleversions import branch_names, branches_known_at, next_branch
from .validate import load_entry

PLUGIN_TYPE_PREFIX = "moodle-"
INSTALLER_PACKAGE = "moodle/composer-installer"

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


def generate(index_dir: str | Path, base_url: str,
             artifacts_base: str | None = None,
             skipped: list[str] | None = None) -> dict:
    """Build the full packages.json document from an index tree."""
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
    return {"packages": packages}


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
          artifacts_base: str | None = None) -> int:
    skipped: list[str] = []
    document = generate(index_dir, base_url, artifacts_base=artifacts_base,
                        skipped=skipped)
    for item in skipped:
        print(f"warning: {item}: version not expressible in Composer's "
              f"grammar; left out of packages.json", file=sys.stderr)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump(document, f, indent=2, sort_keys=True)
        f.write("\n")

    advisories_doc = generate_advisories(index_dir, base_url)
    advisories_path = out.parent / "security-advisories.json"
    with open(advisories_path, "w") as f:
        json.dump(advisories_doc, f, indent=2, sort_keys=True)
        f.write("\n")
    return len(document["packages"])
