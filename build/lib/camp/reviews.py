"""Published security-review feed (MDL Shield): registry-level display.

The registry fetches the reviewer's published-reviews feed once per
publish and renders the result itself — visitors load nothing third-party
(RFC §4.6), and the site never links a review it didn't see in the feed.

The feed's semantics respect the reviewer's privacy model: presence means
"a review exists and its subject chose to publish it"; absence means
nothing at all (unreviewed, private, or unknown are deliberately
indistinguishable), so absence renders as nothing — never a grey
"not reviewed" chip.

Reviews are keyed by frankenstyle component plus the plugin's
$plugin->version integer — the one identifier Moodle itself trusts.
Release strings are display-only. A reviewed version that is not the
release camp currently offers is shown with that caveat, and every chip
carries the deeper one: the reviewer's subject is the moodle.org
distribution of that version, which is not byte-identical to camp's
tag-built artifact.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

DEFAULT_FEED_URL = "https://mdlshield.com/api/reviews/published"
REVIEW_URL_PREFIX = "https://mdlshield.com/"
PLUGIN_URL_PREFIX = "https://mdlshield.com/plugins/"
MAX_FEED_BYTES = 4 * 1024 * 1024
_GRADE_RE = re.compile(r"^[A-F][+-]?$")

# Fallback only: the feed's badge_url embeds MDL Shield's official hex per
# review, which wins when parseable. A/B confirmed from their feed; C–F are
# conservative guesses until they send the full map.
GRADE_COLORS = {
    "A": "#22c55e", "B": "#3b82f6", "C": "#dfb317",
    "D": "#fe7d37", "E": "#e05d44", "F": "#e05d44",
}
_BADGE_COLOR_RE = re.compile(r"-([0-9a-fA-F]{6})$")


def _sanitize_review(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    grade = str(raw.get("grade", "")).strip()[:2]
    if not _GRADE_RE.match(grade):
        return None
    review_url = str(raw.get("review_url", ""))
    if not review_url.startswith(REVIEW_URL_PREFIX):
        review_url = ""
    badge_url = str(raw.get("badge_url", ""))
    if not badge_url.startswith("https://img.shields.io/"):
        badge_url = ""
    color_match = _BADGE_COLOR_RE.search(badge_url)
    return {
        "grade": grade,
        "color": (f"#{color_match.group(1)}" if color_match
                  else GRADE_COLORS[grade[0]]),
        "release": str(raw.get("release", "")).strip()[:40],
        "reviewed_at": str(raw.get("reviewed_at", "")).strip()[:10],
        "review_url": review_url,
        "badge_url": badge_url,
    }


def fetch_feed(source: str) -> dict[str, dict[str, dict]] | None:
    """Fetch and sanitize the published-reviews feed.

    `source` is the feed URL, or a local path (previews, tests). Returns
    {component: {version-int-string: review}} keeping the most recent
    review per version, or None on any failure — a feed we can't read
    means no chips, never stale guesses.
    """
    try:
        if source.startswith("https://"):
            req = urllib.request.Request(
                source, headers={"User-Agent": "camp-review-fetch"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read(MAX_FEED_BYTES)
        else:
            raw = Path(source).read_bytes()[:MAX_FEED_BYTES]
        feed = json.loads(raw)
    except Exception:
        return None
    if not isinstance(feed, dict) or feed.get("schema_version") != 1:
        return None

    out: dict[str, dict[str, dict]] = {}
    plugins = feed.get("plugins")
    if not isinstance(plugins, dict):
        return None
    for component, plugin in plugins.items():
        if not isinstance(plugin, dict):
            continue
        versions = plugin.get("versions")
        if not isinstance(versions, dict):
            continue
        for version, entries in versions.items():
            if not isinstance(entries, list):
                continue
            reviews = [r for r in map(_sanitize_review, entries) if r]
            if not reviews:
                continue
            reviews.sort(key=lambda r: r["reviewed_at"], reverse=True)
            out.setdefault(str(component)[:100], {})[str(version)[:20]] = reviews[0]
    return out


# The strip and Project row show MDL Shield's own badge rendering —
# fetched at publish, sanitized, and served from camp's origin (visitors
# never touch shields.io). Falls back to camp's HTML chip when the badge
# can't be fetched or fails sanitization.
BADGE_URL_PREFIX = "https://img.shields.io/"
MAX_BADGE_BYTES = 64 * 1024
_SVG_NS = "{http://www.w3.org/2000/svg}"


def fetch_badge_svg(url: str) -> bytes | None:
    """Fetch one official badge and sanitize it into a safe static SVG.

    Rendered only ever via <img> (no script execution by spec), but the
    document is still validated as untrusted input: any scripting hook,
    embedded foreign content, or external reference rejects the whole
    badge — the fallback chip renders instead, never a stripped-down
    guess at their artwork."""
    if not url.startswith(BADGE_URL_PREFIX):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "camp-badge-fetch"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read(MAX_BADGE_BYTES)
    except Exception:
        return None
    return sanitize_badge_svg(raw)


def sanitize_badge_svg(raw: bytes) -> bytes | None:
    from xml.etree import ElementTree
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return None
    if root.tag != f"{_SVG_NS}svg":
        return None
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1].lower()
        if tag in ("script", "foreignobject", "use", "image", "animate",
                   "set", "animatetransform", "animatemotion"):
            return None
        for attr, value in el.attrib.items():
            name = attr.rsplit("}", 1)[-1].lower()
            if name.startswith("on"):
                return None
            if name in ("href", "xlink:href") and not str(value).startswith("#"):
                return None
            if "url(" in str(value) and "url(#" not in str(value):
                return None
    return raw
