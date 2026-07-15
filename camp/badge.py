"""Badges: camp's own tier badges, and author-declared endpoint badges.

Two directions, one format — the shields.io "endpoint" schema
(https://shields.io/badges/endpoint-badge, schemaVersion 1):

- **Emit**: every claimed-or-better listing gets /badge/<component>.svg
  (self-hosted flat badge for READMEs) and /badge/<component>.json (the
  endpoint document, for authors who prefer shields.io's renderer).
  Tier 0 gets no badge: the badge is the author's reward for claiming.
- **Consume**: a listing manifest may declare `badges:` entries pointing
  at endpoint-schema URLs on allowlisted hosts (e.g. MDL Shield security
  grades). The site fetches the JSON at publish time and renders the chip
  itself — visitors load nothing third-party (RFC §4.6), a hostile
  endpoint is data to sanitize rather than content to embed, and mirrors
  serve the result like everything else. The cost is publish-time
  freshness, bounded by the scheduled daily publish.
"""

from __future__ import annotations

import json
import re
import urllib.request
from html import escape
from pathlib import Path

from .validate import load_entry

TIER_BADGE_STYLE = {
    1: ("Tier 1 · Claimed", "#6e6a61"),
    2: ("Tier 2 · Verified", "#23854f"),
    3: ("Tier 3 · Reviewed", "#1b6b40"),
}

# Hosts whose endpoint-schema badges listings may declare. Extended by PR;
# an allowlist because fetched URLs run in registry CI and rendered output
# reaches every visitor.
ALLOWED_BADGE_HOSTS = {
    "mdlshield.com",
    "camp-registry.org",
}

_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_NAMED_COLORS = {
    "brightgreen": "#4c1", "green": "#97ca00", "yellowgreen": "#a4a61d",
    "yellow": "#dfb317", "orange": "#fe7d37", "red": "#e05d44",
    "blue": "#007ec6", "lightgrey": "#9f9f9f", "grey": "#555",
}
_MAX_TEXT = 40


def _char_width(text: str) -> int:
    """Approximate rendered width of Verdana 11px text."""
    return int(len(text) * 6.6) + 20


def render_svg(label: str, message: str, color: str) -> str:
    """A shields-style flat badge as a standalone SVG."""
    lw, mw = _char_width(label), _char_width(message)
    w = lw + mw
    lx, mx = lw * 5, lw * 10 + mw * 5   # text centers in scale(.1) space
    label_e, message_e = escape(label), escape(message)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" role="img" aria-label="{label_e}: {message_e}">
<title>{label_e}: {message_e}</title>
<linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/><stop offset="1" stop-opacity=".1"/></linearGradient>
<clipPath id="r"><rect width="{w}" height="20" rx="3" fill="#fff"/></clipPath>
<g clip-path="url(#r)">
<rect width="{lw}" height="20" fill="#555"/>
<rect x="{lw}" width="{mw}" height="20" fill="{color}"/>
<rect width="{w}" height="20" fill="url(#s)"/>
</g>
<g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="110" text-rendering="geometricPrecision">
<text x="{lx}" y="140" transform="scale(.1)" fill="#010101" fill-opacity=".3">{label_e}</text>
<text x="{lx}" y="130" transform="scale(.1)">{label_e}</text>
<text x="{mx}" y="140" transform="scale(.1)" fill="#010101" fill-opacity=".3">{message_e}</text>
<text x="{mx}" y="130" transform="scale(.1)">{message_e}</text>
</g>
</svg>
"""


def endpoint_document(tier: int) -> dict:
    message, color = TIER_BADGE_STYLE[tier]
    return {"schemaVersion": 1, "label": "camp",
            "message": message, "color": color}


def write_badges(index_dir: str | Path, out_dir: str | Path) -> int:
    """Emit svg+json tier badges for every claimed-or-better listing."""
    out = Path(out_dir)
    written = 0
    for entry_path in sorted(Path(index_dir).glob("plugins/*/*.yml")):
        entry = load_entry(entry_path)
        if entry["tier"] < 1 or entry.get("status", "active") == "delisted":
            continue
        doc = endpoint_document(entry["tier"])
        out.mkdir(parents=True, exist_ok=True)
        component = entry["component"]
        (out / f"{component}.json").write_text(
            json.dumps(doc, sort_keys=True) + "\n")
        (out / f"{component}.svg").write_text(
            render_svg(doc["label"], doc["message"], doc["color"]))
        written += 1
    return written


def allowed_endpoint(url: str) -> bool:
    m = re.match(r"https://([^/]+)/", url)
    return bool(m) and m.group(1).lower() in ALLOWED_BADGE_HOSTS


def sanitize_endpoint_document(raw: bytes) -> dict | None:
    """Validate an endpoint-schema response into safe display data."""
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(doc, dict) or doc.get("schemaVersion") != 1:
        return None
    label = str(doc.get("label", ""))[:_MAX_TEXT].strip()
    message = str(doc.get("message", ""))[:_MAX_TEXT].strip()
    if not label or not message:
        return None
    color = str(doc.get("color", "grey")).strip()
    if not _COLOR_RE.match(color):
        color = _NAMED_COLORS.get(color.lower(), "#555")
    return {"label": label, "message": message, "color": color}


def fetch_endpoint(url: str) -> dict | None:
    """Fetch and sanitize one allowlisted endpoint badge. None on any
    failure — a badge the author declared but we can't fetch is omitted,
    never guessed."""
    if not allowed_endpoint(url):
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "camp-badge-fetch"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read(4096)
    except Exception:
        return None
    return sanitize_endpoint_document(raw)
