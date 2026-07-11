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
from pathlib import Path

from .advisory import AdvisorySet
from .validate import load_entry

PLUGIN_TYPE_PREFIX = "moodle-"


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
                       advisories: AdvisorySet | None = None) -> tuple[str, dict]:
    """(package name, {version: definition}) for one index entry. Versions
    revoked by a security advisory (RFC §5.3) are omitted from installation
    metadata; the release ledger and archive are untouched."""
    component = entry["component"]
    name = _package_name(entry)
    versions: dict[str, dict] = {}

    for release in entry["releases"]:
        version = release["version"].split(" ")[0]
        if advisories is not None and advisories.is_revoked(component, version):
            continue
        versions[version] = {
            "name": name,
            "version": version,
            "type": _composer_type(component),
            "license": [entry.get("license", "GPL-3.0-or-later")],
            "dist": {
                "type": "zip",
                "url": f"{base_url}/artifacts/{component}/{component}-{version}.zip",
                "shasum": release["zip-sha256"],
            },
            "source": {
                "type": "git",
                "url": entry["source"],
                "reference": release["commit"],
            },
            "require": {
                "moodle/moodle-composer-installer": "*",
                "php": f">={release.get('php-min', '7.4')}",
            },
            "extra": {
                "camp": {
                    "component": component,
                    "tier": entry["tier"],
                    "labels": entry["labels"],
                    "supported-moodle": release["supported-moodle"],
                    "moodle-version": release["moodle-version"],
                    "published": release["published"],
                },
            },
            "time": release["published"],
        }

    return name, versions


def generate(index_dir: str | Path, base_url: str) -> dict:
    """Build the full packages.json document from an index tree."""
    advisories = AdvisorySet.load(index_dir)
    packages: dict[str, dict] = {}
    for entry_path in sorted(Path(index_dir).glob("plugins/*/*.yml")):
        entry = load_entry(entry_path)
        if entry.get("status", "active") != "active" or entry["tier"] < 1:
            continue
        name, versions = package_definition(entry, base_url, advisories)
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
            "affectedVersions": advisory["affected-versions"],
            "link": f"{base_url}/advisories/{advisory['id']}",
            "cve": advisory.get("cve"),
            "reportedAt": advisory["published"],
            "sources": [{"name": "camp", "remoteId": advisory["id"]}],
        } for advisory in component_advisories]
    return {"advisories": document}


def write(index_dir: str | Path, base_url: str, out_path: str | Path) -> int:
    document = generate(index_dir, base_url)
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
