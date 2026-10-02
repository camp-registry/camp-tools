"""Plugin release maturity (camp-tools#67).

`$plugin->maturity` in version.php is the authority on whether a release is
stable or a pre-release; the `$plugin->release` string may or may not
agree, and disagreement only warns. A release record carries `maturity`
when version.php declared one; absent means stable, as it does for the
rest of the ecosystem. Pre-releases get a Composer stability suffix so a
default install skips them, with an ordinal taken from the release string
when it has one and otherwise assigned in publication order from the
append-only ledger: deterministic, and a second pre-release of the same
version is never refused.
"""
from __future__ import annotations

import re

ORDER = ["alpha", "beta", "rc", "stable"]
STABLE = "stable"

_CONSTANTS = {"MATURITY_ALPHA": "alpha", "MATURITY_BETA": "beta",
              "MATURITY_RC": "rc", "MATURITY_STABLE": "stable",
              "50": "alpha", "100": "beta", "150": "rc", "200": "stable"}
_STRING_RE = re.compile(
    r"^(?P<base>v?\d+(?:\.\d+)*)"
    r"(?:[\s._-]*(?P<mat>alpha|beta|rc|a|b)(?![a-z])[\s._-]*(?P<ord>\d+)?)?",
    re.IGNORECASE)
_SUFFIX = {"alpha": "alpha", "beta": "beta", "rc": "RC"}


def parse_declared(raw: str | None) -> str | None:
    """`$plugin->maturity` as written in version.php (a MATURITY_* constant
    or its numeric value) -> "alpha" | "beta" | "rc" | "stable"; None when
    absent or unrecognised."""
    if not raw:
        return None
    return _CONSTANTS.get(raw.strip().strip("'\""))


def parse_release_string(version: str) -> tuple[str, str | None, int | None]:
    """(base version, maturity the string implies or None, ordinal or
    None). "1.3.0beta2" -> ("1.3.0", "beta", 2); "1.3.0-RC" -> ("1.3.0",
    "rc", None); "1.3.0" -> ("1.3.0", None, None)."""
    version = version.split(" ")[0]
    m = _STRING_RE.match(version)
    if not m:
        return version, None, None
    mat = (m.group("mat") or "").lower()
    mat = {"a": "alpha", "b": "beta"}.get(mat, mat) or None
    ordinal = int(m.group("ord")) if m.group("ord") else None
    return m.group("base"), mat, ordinal


def of_release(release: dict) -> str:
    """The record's maturity; absent means stable."""
    return release.get("maturity") or STABLE


def is_prerelease(release: dict) -> bool:
    return of_release(release) != STABLE


def at_least(release: dict, floor: str) -> bool:
    """True when the record's maturity is `floor` or more mature."""
    return ORDER.index(of_release(release)) >= ORDER.index(floor)


def ordinal(release: dict, releases: list[dict]) -> int:
    """The pre-release's ordinal: from its release string when it has one,
    else its position in publication order among the ledger's records of
    the same base version and maturity (1-based)."""
    base, _, explicit = parse_release_string(str(release["version"]))
    if explicit is not None:
        return explicit
    mat = of_release(release)
    count = 0
    for r in releases:
        if of_release(r) != mat:
            continue
        r_base, _, r_explicit = parse_release_string(str(r["version"]))
        if r_base != base or r_explicit is not None:
            continue
        count += 1
        if r is release or r.get("tag") == release.get("tag"):
            return count
    return count + 1


def composer_version_for(release: dict, releases: list[dict],
                         composer_version) -> str | None:
    """The Composer version for a record: the author's string for a stable
    release (as before); for a pre-release, the base version with the
    stability suffix its maturity implies and the ordinal (`1.3.0-beta2`),
    so Composer's default minimum-stability skips it."""
    version = str(release["version"]).split(" ")[0]
    if not is_prerelease(release):
        return composer_version(version)
    base, _, _ = parse_release_string(version)
    return composer_version(f"{base}-{_SUFFIX[of_release(release)]}{ordinal(release, releases)}")


def label(release: dict, releases: list[dict]) -> str:
    """Human label for a pre-release, e.g. "beta 2"; empty for stable."""
    if not is_prerelease(release):
        return ""
    return f"{of_release(release)} {ordinal(release, releases)}"


def mismatch_notice(declared: str | None, version: str) -> str | None:
    """Warn-only text when the release string and $plugin->maturity
    disagree; None when they agree or the string is silent."""
    _, implied, _ = parse_release_string(version)
    effective = declared or STABLE
    if implied is None or implied == effective:
        return None
    return (f"$plugin->release '{version}' reads as {implied} but "
            f"$plugin->maturity {'is ' + declared if declared else 'is not declared (stable)'}; "
            f"the record follows maturity ({effective}). Align the two in "
            f"version.php if one is stale (camp-tools#67)")
