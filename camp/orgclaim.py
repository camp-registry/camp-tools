"""Organization-level claims.

An organization that owns many listed plugins claims them in one move by
publishing a manifest in a dedicated repository (``<org>/camp-claim``,
file ``camp-claim.yml`` at the root). Control of that repository is the
authorization: it is the same trust root the publisher uses (control of
the source), lifted to the organization that owns every source repo.

The sweep applies the manifest to listed entries whose source lives under
the organization, claiming Tier 0 entries exactly as a claim PR would
(maintainers, security-contact, labels, tier 1) and stamping them
``org-claim: <org>``. Entries carrying that stamp are the manifest's to
keep current: later sweeps re-apply manifest changes to them (and only
the manifest-owned fields — tier and releases are never touched, so
Tier 2 entries update safely). The sweep NEVER touches a claimed entry
without the stamp: an individual claim the manifest would also cover is
reported as a conflict for human review, not overwritten. An entry the
org excludes after claiming is likewise only reported, never un-claimed.

The first sweep for an org is human-gated (request issue, see the
org-claims runbook); after enrollment in ``discovery/org-claims.yml``
the watch workflow re-runs the sweep on a schedule with these same
rules — the stamp is what makes unattended re-runs safe.

Manifest format::

    maintainers:            # required, the accounts that may publish
      - github: someone
    security-contact: https://github.com/org/repo/security   # required
    labels: [fully-free]    # required default, schema vocabulary
    overrides:              # optional, per-component
      local_example:
        labels: [external-account, paid-service]
    exclude: [local_other]  # optional, components the org does not claim
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

USER_AGENT = "camp-tools"
MANIFEST_REPO = "camp-claim"
MANIFEST_FILE = "camp-claim.yml"

# Mirrors the schema's label vocabulary; validated here so a bad manifest
# fails the sweep with a message instead of failing entry validation later.
LABEL_VOCABULARY = {
    "fully-free", "freemium", "paid-service", "external-account",
    "donation-supported", "commercial-support-available",
}

# The canonical key order claim PRs produce; rebuilt on write so org-claimed
# entries match hand-claimed ones apart from the org-claim stamp.
_KEY_ORDER = ["component", "source", "source-repo-id", "security-contact",
              "maintainers", "tier", "labels", "org-claim"]

ENROLLED_FILE = "discovery/org-claims.yml"


class ManifestError(Exception):
    """The manifest is missing or invalid; nothing was changed."""


@dataclass
class OrgClaimReport:
    claimed: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)     # stamped, manifest changed
    conflicts: list[str] = field(default_factory=list)   # individually claimed
    excluded: list[str] = field(default_factory=list)
    excluded_claimed: list[str] = field(default_factory=list)  # stamped, now excluded
    skipped: list[str] = field(default_factory=list)     # non-active status


def load_enrolled(index_dir: str | Path) -> list[str]:
    """Organizations enrolled for the scheduled watch sweep."""
    path = Path(index_dir) / ENROLLED_FILE
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return sorted((data.get("orgs") or {}).keys())


def _fetch_manifest(org: str, manifest_repo: str) -> bytes:
    url = (f"https://raw.githubusercontent.com/{org}/{manifest_repo}"
           f"/HEAD/{MANIFEST_FILE}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ManifestError(
                f"no {MANIFEST_FILE} found at {org}/{manifest_repo}") from exc
        raise


def parse_manifest(raw: bytes) -> dict:
    """Validate manifest bytes; raises ManifestError with a specific message."""
    try:
        manifest = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ManifestError(f"manifest is not valid YAML: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be a YAML mapping")
    maintainers = manifest.get("maintainers")
    if (not isinstance(maintainers, list) or not maintainers or
            not all(isinstance(m, dict) and
                    isinstance(m.get("github"), str) and m["github"].strip()
                    for m in maintainers)):
        raise ManifestError(
            "maintainers must be a non-empty list of {github: account} items")
    contact = manifest.get("security-contact")
    if not isinstance(contact, str) or not contact.strip():
        raise ManifestError("security-contact is required")
    def check_labels(labels, where):
        if (not isinstance(labels, list) or not labels or
                not set(labels) <= LABEL_VOCABULARY):
            raise ManifestError(
                f"{where} labels must be a non-empty subset of "
                f"{sorted(LABEL_VOCABULARY)}")
    check_labels(manifest.get("labels"), "default")
    overrides = manifest.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise ManifestError("overrides must be a mapping of component to settings")
    for component, settings in overrides.items():
        if not isinstance(settings, dict):
            raise ManifestError(f"override for {component} must be a mapping")
        if "labels" in settings:
            check_labels(settings["labels"], component)
    exclude = manifest.get("exclude") or []
    if not isinstance(exclude, list) or not all(isinstance(c, str) for c in exclude):
        raise ManifestError("exclude must be a list of component names")
    return manifest


def _source_org(source: str) -> str | None:
    """The owning organization of a github.com source URL, else None."""
    parsed = urllib.parse.urlparse(source)
    if parsed.netloc.lower() != "github.com":
        return None
    segments = [s for s in parsed.path.split("/") if s]
    return segments[0].lower() if len(segments) >= 2 else None


def _reordered(entry: dict) -> dict:
    ordered = {k: entry[k] for k in _KEY_ORDER if k in entry}
    ordered.update({k: v for k, v in entry.items() if k not in ordered})
    return ordered


def org_claim(index_dir: str | Path, org: str, manifest_repo: str = MANIFEST_REPO,
              fetch=None, dry_run: bool = False) -> OrgClaimReport:
    """Sweep the index, claiming the org's unclaimed entries per its manifest."""
    raw = (fetch or _fetch_manifest)(org, manifest_repo)
    manifest = parse_manifest(raw)
    overrides = manifest.get("overrides") or {}
    exclude = set(manifest.get("exclude") or [])
    report = OrgClaimReport()

    for path in sorted(Path(index_dir).glob("plugins/**/*.yml")):
        entry = yaml.safe_load(path.read_text())
        if _source_org(entry.get("source", "")) != org.lower():
            continue
        component = entry["component"]
        stamped = entry.get("org-claim") == org.lower()
        if component in exclude:
            # Un-claiming is never automatic: a stamped entry the org now
            # excludes is a human conversation, not a tier change.
            (report.excluded_claimed if stamped else report.excluded
             ).append(component)
            continue
        if entry.get("status", "active") != "active":
            report.skipped.append(component)
            continue
        desired = {
            "maintainers": [dict(m) for m in manifest["maintainers"]],
            "security-contact": manifest["security-contact"],
            "labels": list(
                overrides.get(component, {}).get("labels", manifest["labels"])),
        }
        if entry.get("tier", 0) >= 1:
            if not stamped:
                report.conflicts.append(component)
                continue
            # Manifest-owned fields only: tier and releases stay untouched,
            # so re-sweeping a Tier 2 entry is safe.
            if all(entry.get(k) == v for k, v in desired.items()):
                continue
            entry.update(desired)
            report.updated.append(component)
        else:
            entry.update(desired)
            entry["tier"] = 1
            entry["org-claim"] = org.lower()
            report.claimed.append(component)
        if not dry_run:
            with open(path, "w") as f:
                yaml.safe_dump(_reordered(entry), f, sort_keys=False,
                               allow_unicode=True)
    return report
