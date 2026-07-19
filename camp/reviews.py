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

GRADE_COLORS = {
    "A": "#23854f", "B": "#a4a61d", "C": "#dfb317",
    "D": "#fe7d37", "E": "#e05d44", "F": "#e05d44",
}


def _sanitize_review(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    grade = str(raw.get("grade", "")).strip()[:2]
    if not _GRADE_RE.match(grade):
        return None
    review_url = str(raw.get("review_url", ""))
    if not review_url.startswith(REVIEW_URL_PREFIX):
        review_url = ""
    return {
        "grade": grade,
        "color": GRADE_COLORS[grade[0]],
        "release": str(raw.get("release", "")).strip()[:40],
        "reviewed_at": str(raw.get("reviewed_at", "")).strip()[:10],
        "review_url": review_url,
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
