"""Index entry validation (schema + registry invariants)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import yaml

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"


class ValidationError(Exception):
    pass


def load_entry(path: str | Path) -> dict:
    with open(path) as f:
        entry = yaml.safe_load(f)
    if not isinstance(entry, dict):
        raise ValidationError(f"{path}: not a mapping")
    return entry


def newest_release(entry: dict) -> dict | None:
    """Highest-versioned release — NOT the last ledger entry: the ledger is
    append-only in publication order, and backfills append older versions."""
    from .advisory import _version_key
    if not entry.get("releases"):
        return None
    return max(entry["releases"],
               key=lambda r: _version_key(str(r["version"]).split(" ")[0]))


def _schema(name: str) -> dict:
    with open(SCHEMA_DIR / name) as f:
        return json.load(f)


def validate_entry(path: str | Path) -> list[str]:
    """Validate one index entry file. Returns a list of problems (empty = valid)."""
    problems: list[str] = []
    try:
        entry = load_entry(path)
    except (ValidationError, yaml.YAMLError) as exc:
        return [str(exc)]

    validator = jsonschema.Draft202012Validator(_schema("index-entry.schema.json"))
    for error in sorted(validator.iter_errors(entry), key=str):
        location = "/".join(str(p) for p in error.absolute_path) or "(root)"
        problems.append(f"{location}: {error.message}")

    if problems:
        return problems

    # Invariants the schema language can't express.
    component = entry["component"]
    expected_rel = Path(component.partition("_")[0]) / f"{component}.yml"
    actual = Path(path)
    if actual.parts[-2:] != expected_rel.parts:
        problems.append(
            f"file is at {actual.name} under '{actual.parts[-2]}/' but component "
            f"{component} belongs at plugins/{expected_rel}"
        )

    versions = [r["version"] for r in entry["releases"]]
    if len(versions) != len(set(versions)):
        problems.append("duplicate release versions in ledger")
    tags = [r["tag"] for r in entry["releases"]]
    if len(tags) != len(set(tags)):
        problems.append("duplicate release tags in ledger")

    published = [r["published"] for r in entry["releases"]]
    if published != sorted(published):
        problems.append("release ledger is not in chronological order of publication")

    return problems


def validate_utility(path: str | Path) -> list[str]:
    """Validate one utilities/ listing file (camp-docs#4). Returns a list
    of problems (empty = valid)."""
    problems: list[str] = []
    try:
        entry = load_entry(path)
    except (ValidationError, yaml.YAMLError) as exc:
        return [str(exc)]

    validator = jsonschema.Draft202012Validator(_schema("utility.schema.json"))
    for error in sorted(validator.iter_errors(entry), key=str):
        location = "/".join(str(p) for p in error.absolute_path) or "(root)"
        problems.append(f"{location}: {error.message}")
    if problems:
        return problems

    # Invariants the schema language can't express.
    name = entry["name"]
    actual = Path(path).resolve()
    if actual.parts[-2:] != ("utilities", f"{name}.yml"):
        problems.append(
            f"file is at {Path(path)} but utility {name} belongs at "
            f"utilities/{name}.yml")

    # The monitorability fence (camp-docs#4): the canonical distribution
    # channel must be one the registry's tooling observes — the source
    # host enrich monitors natively, or a declared release-channel whose
    # scheme camp-tools implements. The adapter table IS the fence;
    # widening it is a camp-tools change, not an entry-side assertion.
    import urllib.parse
    host = urllib.parse.urlparse(entry["source"]).netloc
    monitored_host = host == "github.com" or "gitlab" in host
    channel = entry.get("release-channel")
    if channel:
        scheme = channel.partition(":")[0]
        from .scan import RELEASE_CHANNELS
        if scheme not in RELEASE_CHANNELS:
            problems.append(
                f"release-channel scheme '{scheme}' is not implemented by "
                f"camp-tools — admission requires a machine-monitorable "
                f"distribution channel")
    elif not monitored_host:
        problems.append(
            f"source host {host} is not monitored by enrich and no "
            f"release-channel is declared — admission requires a "
            f"machine-monitorable distribution channel")

    if entry.get("claimed") and not entry.get("maintainers"):
        problems.append("claimed entries must list maintainers")

    return problems


def validate_listing(path: str | Path) -> list[str]:
    """Validate a .camp/listing.yml manifest. Returns a list of problems."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        return [str(exc)]
    return validate_listing_bytes(raw)


def validate_listing_bytes(raw: bytes) -> list[str]:
    """Validate listing manifest content already in memory (e.g. a git blob
    read at a pinned commit). Returns a list of problems."""
    try:
        listing = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return [str(exc)]
    validator = jsonschema.Draft202012Validator(_schema("listing.schema.json"))
    problems = [
        f"{'/'.join(str(p) for p in error.absolute_path) or '(root)'}: {error.message}"
        for error in sorted(validator.iter_errors(listing), key=str)
    ]
    from .badge import ALLOWED_BADGE_HOSTS, allowed_endpoint
    for i, badge in enumerate((listing or {}).get("badges") or []):
        endpoint = badge.get("endpoint", "") if isinstance(badge, dict) else ""
        if endpoint and not allowed_endpoint(endpoint):
            problems.append(
                f"badges/{i}: endpoint host not in the registry allowlist "
                f"({', '.join(sorted(ALLOWED_BADGE_HOSTS))}) — propose additions "
                f"by PR to camp-tools")
    return problems
