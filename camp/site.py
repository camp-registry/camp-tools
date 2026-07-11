"""Static website generator (RFC §4.5): browse + per-plugin pages.

Renders the index into a plain file tree of HTML — no database, no
application server, mirrorable with the rest of the repository. Design
follows the CAMP mockups (Spectral / IBM Plex, paper + verdigris palette).

Honesty rule: pages only assert what the registry has actually done. The
verification ledger shows the real tag/commit/hash records; signing and
transparency-log steps are rendered as "planned" until they exist.

Listing manifests (name, summary, description) are read from an optional
listings directory (`<component>.yml` files) until the release-time
ingestion pipeline lands. All listing-derived text is HTML-escaped;
markdown rendering with sanitization is deliberately deferred.
"""

from __future__ import annotations

import datetime
from html import escape
from pathlib import Path

import yaml

from .advisory import AdvisorySet
from .composer import _package_name
from .validate import load_entry

PLUGINTYPE_NAMES = {
    "mod": "Activity modules",
    "block": "Blocks",
    "local": "Local plugins",
    "logstore": "Log stores",
    "auth": "Authentication",
    "tool": "Admin tools",
    "theme": "Themes",
    "qtype": "Question types",
    "enrol": "Enrolment",
    "repository": "Repositories",
    "profilefield": "Profile fields",
    "tiny": "TinyMCE editor",
    "atto": "Atto editor",
    "format": "Course formats",
    "report": "Reports",
    "filter": "Filters",
}

TIER_BADGES = {
    0: ('b-note', 'Discovered · Tier 0'),
    1: ('b-free', '✓ Source-verified · Tier 1'),
    2: ('b-rev', '✓ Reviewed · Tier 2'),
}

LABEL_TEXT = {
    "fully-free": "Fully free",
    "freemium": "Freemium",
    "paid-service": "Requires paid service",
    "external-account": "External account",
    "donation-supported": "Donation-supported",
    "commercial-support-available": "Commercial support",
}

CSS = """
  :root{
    --paper:#F7F8F6; --card:#fff; --ink:#1B2430; --muted:#5A6472;
    --line:#E1E5E4; --verd:#1E6E5C; --verd-soft:#E7F1EE; --verd-line:#CDE2DC;
    --amber:#B87A1F; --amber-soft:#F7EEDD; --amber-line:#EBDBBB;
    --mono:'IBM Plex Mono','SF Mono',Menlo,monospace;
    --disp:'Spectral',Georgia,serif;
  }
  *{box-sizing:border-box;margin:0}
  body{background:var(--paper);color:var(--ink);font:15px/1.5 'IBM Plex Sans',-apple-system,sans-serif}
  a{color:var(--verd)}
  .wrap{max-width:760px;margin:0 auto;padding:0 16px 56px}
  .top{background:var(--card);border-bottom:1px solid var(--line)}
  .top-in{max-width:760px;margin:0 auto;padding:10px 16px;display:flex;gap:12px;align-items:center}
  .logo{text-decoration:none;color:var(--ink)}
  .logo b{font-family:var(--disp);font-weight:700;font-size:18px;display:block}
  .logo small{font-size:9px;letter-spacing:.08em;color:var(--muted);text-transform:uppercase}
  .top input{flex:1;border:1px solid var(--line);border-radius:6px;padding:8px 12px;font:inherit;background:var(--paper)}
  header.p{padding:20px 0 14px}
  .crumb{font-size:11px;color:var(--muted);letter-spacing:.05em;text-transform:uppercase}
  h1{font-family:var(--disp);font-weight:600;font-size:30px;line-height:1.1;margin-top:6px}
  .comp{font-family:var(--mono);font-size:12.5px;color:var(--muted);margin-top:2px}
  .tagline{margin-top:8px;color:#333c49}
  .badges{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px}
  .badge{font-size:12px;font-weight:600;padding:4px 10px;border-radius:999px}
  .b-rev{background:var(--verd);color:#fff}
  .b-free{background:var(--verd-soft);color:var(--verd);border:1px solid var(--verd-line)}
  .b-note{background:var(--amber-soft);color:var(--amber);border:1px solid var(--amber-line)}
  .actions{display:flex;gap:10px;margin-top:14px;align-items:center;flex-wrap:wrap}
  .pill{background:var(--verd-soft);border:1px solid var(--verd-line);color:var(--verd);border-radius:8px;padding:9px 13px;font-size:13.5px;font-weight:600}
  .install-btn{background:var(--verd);color:#fff;border:0;border-radius:8px;padding:10px 18px;font:600 14px 'IBM Plex Sans',sans-serif;cursor:pointer}
  .updated{font-size:12.5px;color:var(--muted)}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
  h2{font-family:var(--disp);font-weight:600;font-size:18px;margin:22px 0 10px}
  #installPanel{display:none;margin-top:12px}
  #installPanel.open{display:block}
  .cmd{display:flex;align-items:center;gap:8px;background:#10241F;color:#D8EAE4;border-radius:8px;padding:11px 13px;font-family:var(--mono);font-size:12px;overflow-x:auto}
  .cmd button{margin-left:auto;flex:none;background:var(--verd);color:#fff;border:0;border-radius:6px;padding:5px 10px;font:600 12px 'IBM Plex Sans',sans-serif;cursor:pointer}
  .alt{display:flex;gap:10px;margin-top:10px;font-size:13px;flex-wrap:wrap}
  .alt a{color:var(--ink);font-weight:600;border:1px solid var(--line);border-radius:6px;padding:7px 11px;text-decoration:none;background:var(--paper)}
  .hash{margin-top:10px;font-family:var(--mono);font-size:10.5px;color:var(--muted);word-break:break-all}
  .tabs{display:flex;gap:2px;border-bottom:1px solid var(--line)}
  .tabs button{background:none;border:0;cursor:pointer;font:inherit;font-size:14px;color:var(--muted);padding:10px 12px;border-bottom:2px solid transparent;margin-bottom:-1px}
  .tabs button[aria-selected="true"]{font-weight:600;color:var(--ink);border-bottom-color:var(--verd)}
  .panel{display:none}.panel.active{display:block}
  .row{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--line);font-size:13.5px;align-items:baseline}
  .row:last-child{border-bottom:0}
  .mono{font-family:var(--mono)}
  .ok{color:var(--verd);font-weight:600}
  .row a{font-weight:600;text-decoration:none}
  .ledger{position:relative;padding-left:26px}
  .ledger::before{content:"";position:absolute;left:8px;top:10px;bottom:10px;width:1px;background:var(--line)}
  .step{position:relative;padding:9px 0}
  .step::before{content:"✓";position:absolute;left:-26px;top:11px;width:17px;height:17px;border-radius:50%;
    background:var(--verd);color:#fff;font-size:10px;line-height:17px;text-align:center;font-weight:700}
  .step.pending::before{content:"○";background:var(--paper);color:var(--muted);border:1px solid var(--line);line-height:15px}
  .step.pending h3,.step.pending p{color:var(--muted)}
  .step h3{font-size:13.5px;font-weight:600}
  .step p{font-family:var(--mono);font-size:11.5px;color:var(--muted);margin-top:2px;word-break:break-all}
  .ledger-note{margin-top:10px;padding-top:10px;border-top:1px dashed var(--line);font-size:12.5px;color:var(--muted)}
  .adv{border-left:3px solid var(--verd);background:var(--verd-soft);border-radius:0 8px 8px 0;padding:12px 14px;font-size:13.5px}
  .adv.open{border-left-color:var(--amber);background:var(--amber-soft)}
  .adv .id{font-family:var(--mono);font-size:11px;color:var(--muted);display:block;margin-top:3px}
  footer{margin-top:40px;padding-top:14px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
  .plist{display:flex;flex-direction:column;gap:10px;margin-top:16px}
  .pcard{display:block;text-decoration:none;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
  .pcard:hover{border-color:var(--verd-line)}
  .pcard .nm{font-family:var(--disp);font-weight:600;font-size:17px}
  .pcard .cp{font-family:var(--mono);font-size:11.5px;color:var(--muted);margin-left:8px}
  .pcard .sm{color:#333c49;font-size:13.5px;margin-top:3px}
  .pcard .meta{display:flex;gap:7px;margin-top:8px;flex-wrap:wrap;align-items:center}
  .pcard .meta .updated{margin-left:auto}
  .count{font-size:12.5px;color:var(--muted);margin-top:18px}
"""

FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
    '<link href="https://fonts.googleapis.com/css2?family=Spectral:wght@600;700'
    '&family=IBM+Plex+Sans:wght@400;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">'
)

FOOTER = (
    "CAMP is a community-governed archive of plugins for Moodle™. "
    "Not affiliated with or endorsed by Moodle Pty Ltd."
)

TABS_JS = """
document.querySelectorAll('.tabs button').forEach(function(btn){
  btn.addEventListener('click', function(){
    document.querySelectorAll('.tabs button').forEach(function(b){ b.setAttribute('aria-selected','false'); });
    document.querySelectorAll('.panel').forEach(function(p){ p.classList.remove('active'); });
    btn.setAttribute('aria-selected','true');
    document.getElementById(btn.dataset.tab).classList.add('active');
  });
});
"""

SEARCH_JS = """
var q = document.getElementById('q');
q.addEventListener('input', function(){
  var needle = q.value.toLowerCase();
  var n = 0;
  document.querySelectorAll('.pcard').forEach(function(card){
    var hit = card.dataset.text.indexOf(needle) !== -1;
    card.style.display = hit ? '' : 'none';
    if (hit) n++;
  });
  document.getElementById('count').textContent = n + ' plugin' + (n === 1 ? '' : 's');
});
"""


def _page(title: str, body: str, root: str, script: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(title)}</title>
{FONTS}
<style>{CSS}</style>
</head>
<body>
<div class="top"><div class="top-in">
  <a class="logo" href="{root}index.html"><b>CAMP</b><small>plugin archive</small></a>
</div></div>
<div class="wrap">
{body}
<footer>{FOOTER}</footer>
</div>
{f'<script>{script}</script>' if script else ''}
</body>
</html>
"""


def _fmt_date(iso: str) -> str:
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d %b %Y")


def _moodle_range(release: dict) -> str:
    supported = release["supported-moodle"]
    return supported[0] if len(supported) == 1 else f"{supported[0]} – {supported[-1]}"


def _badges(entry: dict) -> str:
    cls, text = TIER_BADGES[entry["tier"]]
    out = [f'<span class="badge {cls}">{text}</span>']
    out += [
        f'<span class="badge b-free">{escape(LABEL_TEXT.get(label, label))}</span>'
        for label in entry.get("labels", [])
    ]
    return "".join(out)


def _load_listing(listings_dir: Path | None, component: str) -> dict:
    if listings_dir:
        path = listings_dir / f"{component}.yml"
        if path.exists():
            with open(path) as f:
                return yaml.safe_load(f) or {}
    return {}


def _render_description(text: str) -> str:
    """Render a listing description as sanitized markdown (RFC §4.1:
    "sanitized markdown with no raw HTML").

    markdown-it-py in commonmark mode with html=False treats raw HTML as
    text (it arrives escaped in the output), and its default link validator
    rejects javascript:/vbscript:/data: URLs. Images are disabled outright —
    screenshots are the only sanctioned image channel (they get re-encoded
    by ingestion); a hotlinked <img> in a description would be a tracking
    vector on every page view.
    """
    from markdown_it import MarkdownIt
    md = MarkdownIt("commonmark", {"html": False})
    md.disable("image")
    return md.render(text)


def _zip_url(base_url: str, component: str, version: str) -> str:
    return f"{base_url}/artifacts/{component}/{component}-{version}.zip"


def _advisory_cards(component: str, advisories: AdvisorySet) -> str:
    items = advisories.for_component(component)
    if not items:
        return '<div class="adv"><b>No published advisories</b></div>'
    cards = []
    for advisory in sorted(items, key=lambda a: a["id"], reverse=True):
        fixed = advisory.get("fixed-in")
        status = f"fixed in {escape(fixed)}" if fixed else "no fixed version"
        if advisory.get("revoke"):
            status += " · affected versions revoked from installation"
        cards.append(
            f'<div class="adv open"><b>{escape(advisory["severity"].upper())}: '
            f'{escape(advisory["title"])}</b>'
            f'<span class="id">{escape(advisory["id"])} · affects '
            f'{escape(advisory["affected-versions"])} · {status}</span></div>')
    return "\n".join(cards)


def _detail_page(entry: dict, listing: dict, base_url: str,
                 advisories: AdvisorySet) -> str:
    component = entry["component"]
    plugintype = component.partition("_")[0]
    name = listing.get("name") or component
    summary = listing.get("summary") or entry.get("summary") or ""
    latest = entry["releases"][-1] if entry["releases"] else None
    package = _package_name(entry)
    maintainer = entry["maintainers"][0]
    maintainer_name = maintainer.get("name") or maintainer.get("github") or "maintainer"

    header = f"""
<header class="p">
  <div class="crumb">{escape(PLUGINTYPE_NAMES.get(plugintype, plugintype))}</div>
  <h1>{escape(name)}</h1>
  {f'<div class="comp">{escape(component)}</div>' if name != component else ''}
  {f'<p class="tagline">{escape(summary)}</p>' if summary else ''}
  <div class="badges">{_badges(entry)}</div>
"""
    if latest:
        version = latest["version"].lstrip("v")
        cmd = f"composer require {package}"
        header += f"""
  <div class="actions">
    <span class="pill">✓ Works with Moodle {escape(_moodle_range(latest))}</span>
    <button class="install-btn" onclick="document.getElementById('installPanel').classList.toggle('open')">Install</button>
    <span class="updated">Updated {_fmt_date(latest["published"])}</span>
  </div>
  <div id="installPanel" class="card">
    <div class="cmd"><span>{escape(cmd)}</span>
      <button onclick="navigator.clipboard&&navigator.clipboard.writeText('{escape(cmd)}');this.textContent='Copied'">Copy</button></div>
    <div class="alt"><a href="{escape(_zip_url(base_url, component, version))}">Download ZIP · v{escape(version)}</a>
      <a href="{escape(entry["source"])}">Source repository</a></div>
    <div class="hash">sha256 {latest["zip-sha256"]}</div>
  </div>
"""
    if entry["tier"] == 0:
        header += """
  <div class="card" style="margin-top:14px;border-left:3px solid var(--amber);border-radius:0 10px 10px 0;font-size:13.5px">
    <b>Discovered listing.</b> Found by scanning public sources; nothing is hosted here —
    installation happens from the author's own repository. Are you the maintainer?
    <a href="#">Claim this plugin</a> to publish verified releases, or
    <a href="#">request removal</a> — no questions asked.
  </div>
"""
    header += "</header>"

    description = listing.get("description") or ""
    about_html = (
        _render_description(description)
        or f'<p>No listing manifest published yet. See the <a href="{escape(entry["source"])}">source repository</a> for documentation.</p>'
    )
    overview = f"""
<div class="panel active" id="overview">
  <h2>About</h2>
  <div class="card">{about_html}</div>
  <h2>Maintainer</h2>
  <div class="card" style="display:flex;justify-content:space-between;align-items:center;gap:10px">
    <div><b>{escape(maintainer_name)}</b>
      <div style="font-size:12.5px;color:var(--muted)">{len(entry["releases"])} release{"s" if len(entry["releases"]) != 1 else ""} in the archive</div></div>
    <a href="{escape(entry["source"])}" style="font-weight:600;font-size:13px;text-decoration:none">Source &amp; issues →</a>
  </div>
</div>
"""

    version_rows = "".join(
        f"""<div class="row"><span class="mono" style="font-weight:600;min-width:48px">{escape(r["version"].lstrip("v"))}</span>
<span style="color:var(--muted);flex:1">{_fmt_date(r["published"])} · Moodle {escape(_moodle_range(r))}</span>
<a href="{escape(_zip_url(base_url, component, r["version"].lstrip("v")))}">ZIP</a></div>"""
        for r in reversed(entry["releases"])
    ) or '<div class="row"><span style="color:var(--muted)">No releases yet (discovered listing)</span></div>'
    versions = f"""
<div class="panel" id="versions">
  <h2>Release history</h2>
  <div class="card" style="padding:4px 16px">{version_rows}</div>
</div>
"""

    if latest:
        ledger_steps = f"""
      <div class="step"><h3>Source tagged by maintainer</h3><p>{escape(entry["source"].removeprefix("https://"))} @ {escape(latest["tag"])} · commit {latest["commit"][:12]}</p></div>
      <div class="step"><h3>Rebuilt deterministically from that tag</h3><p>canonical ZIP, byte-identical on every rebuild</p></div>
      <div class="step"><h3>Artifact hash recorded in the public index</h3><p>sha256 {latest["zip-sha256"][:16]}…{latest["zip-sha256"][-8:]}</p></div>
      <div class="step pending"><h3>Release signed · trusted publishing</h3><p>planned — TUF signing (RFC §4.3)</p></div>
      <div class="step pending"><h3>Recorded in public transparency log</h3><p>planned — Sigstore/Rekor (RFC §4.3)</p></div>
"""
    else:
        ledger_steps = '<div class="step pending"><h3>No artifacts at Tier 0</h3><p>discovered listing; metadata only</p></div>'
    trust = f"""
<div class="panel" id="trust">
  <h2>Verification ledger</h2>
  <div class="card">
    <div class="ledger">{ledger_steps}</div>
    <div class="ledger-note">Every step is independently verifiable. CAMP never modifies plugin code; it proves the ZIP you install is exactly what the maintainer published.</div>
  </div>
  <h2>Security advisories</h2>
  {_advisory_cards(component, advisories)}
</div>
"""

    body = f"""
{header}
<div class="tabs" role="tablist">
  <button role="tab" aria-selected="true" data-tab="overview">Overview</button>
  <button role="tab" aria-selected="false" data-tab="versions">Versions</button>
  <button role="tab" aria-selected="false" data-tab="trust">Trust &amp; security</button>
</div>
{overview}
{versions}
{trust}
"""
    return _page(f"{name} — CAMP", body, root="../", script=TABS_JS)


def _browse_page(entries: list[tuple[dict, dict]]) -> str:
    cards = []
    for entry, listing in entries:
        component = entry["component"]
        name = listing.get("name") or component
        summary = listing.get("summary") or entry.get("summary") or ""
        latest = entry["releases"][-1] if entry["releases"] else None
        haystack = escape(f"{name} {component} {summary}".lower())
        meta = _badges(entry)
        if latest:
            meta += f'<span class="updated">Moodle {escape(_moodle_range(latest))} · updated {_fmt_date(latest["published"])}</span>'
        comp_tag = f'<span class="cp">{escape(component)}</span>' if name != component else ""
        cards.append(f"""
<a class="pcard" href="plugin/{escape(component)}.html" data-text="{haystack}">
  <span class="nm">{escape(name)}</span>{comp_tag}
  {f'<div class="sm">{escape(summary)}</div>' if summary else ''}
  <div class="meta">{meta}</div>
</a>""")

    body = f"""
<header class="p">
  <h1>Browse plugins</h1>
  <p class="tagline">Every plugin is automatically verified to match its public source. No accounts, no tracking, mirrorable by anyone.</p>
</header>
<input type="search" id="q" placeholder="Search {len(entries)} plugins…" aria-label="Search plugins"
  style="width:100%;border:1px solid var(--line);border-radius:8px;padding:11px 14px;font:inherit;background:var(--card)">
<div class="plist">{''.join(cards)}</div>
<div class="count" id="count">{len(entries)} plugin{"s" if len(entries) != 1 else ""}</div>
"""
    return _page("CAMP — plugin archive", body, root="./", script=SEARCH_JS)


def generate(index_dir: str | Path, base_url: str, out_dir: str | Path,
             listings_dir: str | Path | None = None) -> int:
    out = Path(out_dir)
    (out / "plugin").mkdir(parents=True, exist_ok=True)
    listings = Path(listings_dir) if listings_dir else None
    advisories = AdvisorySet.load(index_dir)

    entries: list[tuple[dict, dict]] = []
    for entry_path in sorted(Path(index_dir).glob("plugins/*/*.yml")):
        entry = load_entry(entry_path)
        if entry.get("status", "active") != "active":
            continue
        listing = _load_listing(listings, entry["component"])
        entries.append((entry, listing))

    entries.sort(key=lambda pair: (pair[1].get("name") or pair[0]["component"]).lower())

    for entry, listing in entries:
        page = _detail_page(entry, listing, base_url, advisories)
        (out / "plugin" / f"{entry['component']}.html").write_text(page)

    (out / "index.html").write_text(_browse_page(entries))
    return len(entries)
