"""Index entry validation (schema + registry invariants)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import yaml

SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "schema"


class ValidationError(Exception):
    pass


def load_entry(path: str | Path) -> dict:
    with open(path) as f:
        entry = yaml.safe_load(f)
    if not isinstance(entry, dict):
        raise ValidationError(f"{path}: not a mapping")
    return entry


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


def validate_listing(path: str | Path) -> list[str]:
    """Validate a .camp/listing.yml manifest. Returns a list of problems."""
    try:
        with open(path) as f:
            listing = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        return [str(exc)]
    validator = jsonschema.Draft202012Validator(_schema("listing.schema.json"))
    return [
        f"{'/'.join(str(p) for p in error.absolute_path) or '(root)'}: {error.message}"
        for error in sorted(validator.iter_errors(listing), key=str)
    ]
