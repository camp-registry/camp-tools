"""Index entry validation (schema + registry invariants)."""

from __future__ import annotations

import difflib
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


def newest_stable_release(entry: dict) -> dict | None:
    """Highest-versioned release whose maturity is stable (camp-tools#67);
    the newest pre-release only when the entry has no stable release at
    all. What every default (page, install card, Composer consumers) shows."""
    from . import maturity
    stable = [r for r in entry.get("releases") or [] if not maturity.is_prerelease(r)]
    if not stable:
        return newest_release(entry)
    return newest_release({"releases": stable})


def _schema(name: str) -> dict:
    with open(SCHEMA_DIR / name) as f:
        return json.load(f)


# What a claim adds to a Tier 0 entry (AUTHORS.md Step 1). Hand-authored
# claim PRs are where schema errors bite authors: three of them stumbled
# on these exact keys (camp-index#82/#83, #399-#401; camp-tools#12).
CLAIM_KEYS = ("maintainers", "security-contact", "labels")
AUTHORS_URL = "https://github.com/camp-registry/camp-docs/blob/main/AUTHORS.md"


def _describe(error: jsonschema.ValidationError) -> str:
    """The schema error message, plus a did-you-mean for unknown keys
    (camp-tools#12): the schema rightly rejects `security`, but the author
    needs to hear `security-contact`."""
    message = error.message
    if error.validator == "additionalProperties" and isinstance(error.instance, dict):
        known = list((error.schema.get("properties") or {}).keys())
        for key in sorted(set(error.instance) - set(known)):
            match = difflib.get_close_matches(str(key), known, n=1, cutoff=0.6)
            if match:
                message += f" (did you mean '{match[0]}'?)"
    return message


def _claim_hint(entry: dict, errors: list) -> str | None:
    """One line for the common claim mistake: a Tier 1+ entry missing or
    misspelling a claim key gets the checklist, not just the schema's
    'required property' (camp-tools#12)."""
    try:
        tier = int(entry.get("tier") or 0)
    except (TypeError, ValueError):
        return None
    if tier < 1:
        return None
    at_root = [e for e in errors if not list(e.absolute_path)]
    missing = any(e.validator == "required" and any(k in e.message for k in CLAIM_KEYS)
                  for e in at_root)
    unknown = any(e.validator == "additionalProperties" for e in at_root)
    if not (missing or unknown):
        return None
    return ("a claim adds exactly these to the entry: maintainers (you), "
            "security-contact, labels, and tier: 1; releases stay as they are "
            f"(AUTHORS.md Step 1, {AUTHORS_URL})")


def validate_entry(path: str | Path) -> list[str]:
    """Validate one index entry file. Returns a list of problems (empty = valid)."""
    problems: list[str] = []
    try:
        entry = load_entry(path)
    except (ValidationError, yaml.YAMLError) as exc:
        return [str(exc)]

    validator = jsonschema.Draft202012Validator(_schema("index-entry.schema.json"))
    errors = sorted(validator.iter_errors(entry), key=str)
    for error in errors:
        location = "/".join(str(p) for p in error.absolute_path) or "(root)"
        problems.append(f"{location}: {_describe(error)}")
    hint = _claim_hint(entry, errors)
    if hint:
        problems.append(f"hint: {hint}")
    if isinstance(entry, dict) and "metrics" in entry:
        # Since camp-tools#70 train two the block has one home; the schema
        # already rejects it, this says where it goes.
        problems.append("hint: metrics live in metrics/<type>/<component>.yml, "
                        "not in the entry; `camp migrate-metrics INDEX` moves the block")
    # The entry's metrics sidecar, when it has one (camp-tools#70): validated
    # with the entry so a broken sidecar surfaces wherever the entry does.
    if isinstance(entry.get("component"), str):
        sidecar = (Path(path).resolve().parent.parent.parent / "metrics"
                   / entry["component"].partition("_")[0] / f"{entry['component']}.yml")
        if sidecar.exists():
            problems.extend(f"metrics sidecar: {p}" for p in validate_metrics(sidecar)
                            if not p.startswith("orphan"))

    if problems:
        return problems

    # Invariants the schema language can't express.
    component = entry["component"]
    expected_rel = Path(component.partition("_")[0]) / f"{component}.yml"
    actual = Path(path)
    if actual.parts[-2:] != expected_rel.parts:
        problems.append(
            f"file is at {actual.name} under '{actual.parent}/' but component "
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


def validate_metrics(path: str | Path) -> list[str]:
    """A metrics sidecar (metrics/<type>/<component>.yml, camp-tools#70):
    the metrics schema, and an entry it belongs to (an orphan sidecar would
    keep serving signals for a removed listing)."""
    import jsonschema
    path = Path(path)
    try:
        with open(path) as f:
            doc = yaml.safe_load(f)
    except Exception as exc:
        return [f"unreadable: {exc}"]
    if not isinstance(doc, dict):
        return ["not a mapping"]
    validator = jsonschema.Draft202012Validator(_schema("metrics.schema.json"))
    problems = []
    for error in sorted(validator.iter_errors(doc), key=str):
        location = "/".join(str(x) for x in error.absolute_path) or "(root)"
        problems.append(f"{location}: {_describe(error)}")
    component = path.stem
    index_dir = path.resolve().parent.parent.parent
    entry_path = index_dir / "plugins" / component.partition("_")[0] / f"{component}.yml"
    if not entry_path.exists():
        problems.append(f"orphan: no entry plugins/{component.partition('_')[0]}/{component}.yml for this sidecar")
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
