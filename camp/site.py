"""Static website generator (RFC §4.5): browse + per-plugin pages.

Renders the index into a plain file tree of HTML — no database, no
application server, mirrorable with the rest of the repository. Design
follows the claude.com/design handoff ("CAMP Redesign"): oklch token
system with a light/dark theme, Playfair Display / Mulish / IBM Plex Mono
(self-hosted woff2 subsets — no font CDN calls, RFC §4.6), an editorial
masthead, a faceted filter sidebar, and per-plugin detail pages. One
deliberate adaptation from the handoff: the plugin detail is a real page
(linkable, mirror-friendly, works without JS) rather than a slide-over.

Honesty rule: pages only assert what the registry has actually done. The
verification block states real dates from the ledger; signing and
transparency-log steps are described as planned until they exist.

Listing manifests (name, summary, description) are read from an optional
listings directory (`<component>.yml` files) until the release-time
ingestion pipeline lands. All listing-derived text is HTML-escaped;
markdown rendering with sanitization is deliberately restricted.
"""

from __future__ import annotations

import datetime
import json
import shutil
from collections import Counter
from html import escape
from pathlib import Path

import yaml

from . import __version__ as TOOLS_VERSION
from .advisory import AdvisorySet
from . import badge as badge_mod
from . import checks as checks_mod
from . import reviews as reviews_mod
from .reviews import PLUGIN_URL_PREFIX as MDLSHIELD_PLUGIN_URL
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

TIER_NAMES = {
    0: 'Discovered',
    1: 'Claimed',
    2: 'Verified',   # 'source-verified' in full — the docs say what it means
    3: 'Reviewed',
}

LABEL_TEXT = {
    "fully-free": "Fully free",
    "freemium": "Freemium",
    "paid-service": "Requires paid service",
    "external-account": "External account",
    "donation-supported": "Donation-supported",
    "commercial-support-available": "Commercial support",
}

# Ordered Moodle branches for range filtering (oldest → newest) — derived
# from the one source of truth. (The old hand-copied list had silently
# dropped 3.10.)
from .moodleversions import branch_names as _branch_names
VORDER = _branch_names()

MIRROR_URL = "https://github.com/camp-registry/camp-docs/blob/main/MIRRORING.md"
INDEX_REPO_URL = "https://github.com/camp-registry/camp-index"
AUTHORS_GUIDE_URL = "https://github.com/camp-registry/camp-docs/blob/main/AUTHORS.md"

FONT_FILES = {
    ("Playfair Display", 500): "playfair-display-v40-latin-500.woff2",
    ("Playfair Display", 600): "playfair-display-v40-latin-600.woff2",
    ("Mulish", 400): "mulish-v18-latin-regular.woff2",
    ("Mulish", 500): "mulish-v18-latin-500.woff2",
    ("Mulish", 600): "mulish-v18-latin-600.woff2",
    ("Mulish", 700): "mulish-v18-latin-700.woff2",
    ("IBM Plex Mono", 400): "ibm-plex-mono-v20-latin-regular.woff2",
    ("IBM Plex Mono", 500): "ibm-plex-mono-v20-latin-500.woff2",
    ("IBM Plex Mono", 600): "ibm-plex-mono-v20-latin-600.woff2",
}

FONT_CSS = "\n".join(
    "@font-face{font-family:'%s';font-style:normal;font-weight:%d;"
    "src:url('/fonts/%s') format('woff2');font-display:swap}" % (fam, w, f)
    for (fam, w), f in FONT_FILES.items()
)

# ---------------------------------------------------------------- tokens ---

CSS = FONT_CSS + """
:root{
  --bg:oklch(0.965 0.008 85); --surface:oklch(0.99 0.006 85);
  --ink:oklch(0.24 0.012 65); --ink-soft:oklch(0.29 0.012 65); --text:oklch(0.34 0.012 68);
  --muted:oklch(0.44 0.012 70); --faint-label:oklch(0.52 0.012 70);
  --faint:oklch(0.57 0.012 72); --faint-2:oklch(0.72 0.01 80);
  --border:oklch(0.88 0.01 80); --border-strong:oklch(0.64 0.012 80);
  --accent:oklch(0.52 0.12 264); --accent-hover:oklch(0.42 0.13 264); --accent-soft:oklch(0.93 0.02 264);
  --green:oklch(0.53 0.11 150); --green-text:oklch(0.47 0.09 150);
  --green-border:oklch(0.85 0.03 150); --green-bg:oklch(0.97 0.02 150);
  --green-head:oklch(0.4 0.09 150); --green-body:oklch(0.38 0.04 150);
  --amber:oklch(0.62 0.12 65); --red:oklch(0.55 0.15 30);
  --text-secondary:var(--muted); --text-subtle:oklch(0.5 0.012 72);
  --focus:oklch(0.52 0.12 264);
  --ok-text:oklch(0.48 0.1 150); --warn-text:oklch(0.47 0.1 65); --bad-text:oklch(0.47 0.16 30);
  --ok-fill:oklch(0.53 0.11 150); --warn-fill:oklch(0.48 0.12 65); --bad-fill:oklch(0.55 0.15 30);
  --scrim:oklch(0.24 0.012 65 / 0.42); --shadow:oklch(0.24 0.012 65 / 0.14);
  --mono:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
  --serif:'Playfair Display',Georgia,serif;
  --sans:'Mulish',-apple-system,'Segoe UI',sans-serif;
}
[data-theme="dark"]{
  --bg:oklch(0.185 0.006 75); --surface:oklch(0.225 0.007 75);
  --ink:oklch(0.93 0.008 85); --ink-soft:oklch(0.86 0.008 85); --text:oklch(0.8 0.008 82);
  --muted:oklch(0.68 0.008 80); --faint-label:oklch(0.63 0.008 78);
  --faint:oklch(0.58 0.008 78); --faint-2:oklch(0.5 0.008 78);
  --border:oklch(0.32 0.008 75); --border-strong:oklch(0.56 0.008 75);
  --accent:oklch(0.74 0.12 264); --accent-hover:oklch(0.82 0.12 264); --accent-soft:oklch(0.3 0.045 264);
  --green:oklch(0.62 0.13 150); --green-text:oklch(0.74 0.11 150);
  --green-border:oklch(0.42 0.06 150); --green-bg:oklch(0.27 0.05 150);
  --green-head:oklch(0.78 0.1 150); --green-body:oklch(0.74 0.06 150);
  --amber:oklch(0.74 0.12 65); --red:oklch(0.7 0.15 30);
  --text-secondary:var(--muted); --text-subtle:oklch(0.66 0.008 78);
  --focus:oklch(0.74 0.12 264);
  --ok-text:oklch(0.72 0.11 150); --warn-text:oklch(0.74 0.12 65); --bad-text:oklch(0.72 0.14 30);
  --ok-fill:oklch(0.5 0.11 150); --warn-fill:oklch(0.48 0.12 65); --bad-fill:oklch(0.52 0.15 30);
  --scrim:oklch(0.1 0.006 75 / 0.62); --shadow:oklch(0 0 0 / 0.55);
}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--text);font:0.9375rem/1.5 var(--sans);
  transition:background .2s,color .2s}
a{color:var(--accent);text-decoration:none}
a:hover{color:var(--accent-hover)}
.mono{font-family:var(--mono)}
.visually-hidden{position:absolute;width:1px;height:1px;padding:0;margin:-1px;
  overflow:hidden;clip-path:inset(50%);white-space:nowrap;border:0}
.skip-link{position:absolute;left:-9999px;top:0;z-index:70;
  background:var(--surface);color:var(--ink);padding:10px 16px;
  border:1px solid var(--border-strong);border-radius:2px;font:0.8125rem var(--mono)}
.skip-link:focus{left:12px;top:12px}
:where(a,button,input,select,summary,[tabindex]):focus-visible{
  outline:3px solid var(--focus);outline-offset:3px}
.cmdline :focus-visible{outline-color:var(--bg)}
.facet:focus-visible,.facet-more:focus-visible{outline-offset:-3px}
.skip-inline:focus{position:static;display:inline-block;margin-bottom:10px}

/* ---- header / nav ---- */
.wrap{max-width:1180px;margin:0 auto;padding:0 32px}
.narrow{max-width:860px;margin:0 auto;padding:34px 32px 90px}
.topbar{display:flex;justify-content:space-between;align-items:baseline;
  flex-wrap:wrap;row-gap:10px;
  padding:22px 0;border-bottom:1px solid var(--border)}
.wordmark{display:flex;align-items:baseline;gap:14px;color:var(--ink)}
.wordmark:hover{color:var(--ink)}
.wordmark b{font-family:var(--mono);font-weight:600;font-size:1.375rem;letter-spacing:.14em}
.wordmark small{font-family:var(--mono);font-size:0.75rem;
  letter-spacing:.07em;color:var(--faint-label)}
nav{display:flex;align-items:center;flex-wrap:wrap;gap:2px 22px;
  font-family:var(--mono);font-size:0.8125rem}
nav a{color:var(--muted)}
nav a:hover{color:var(--ink)}
.theme-toggle{background:none;border:0;cursor:pointer;line-height:0;
  color:var(--muted);display:inline-flex;align-items:center;justify-content:center;
  min-width:44px;min-height:44px;padding:8px;margin:-13px -8px}
.theme-toggle:hover{color:var(--ink)}
.theme-toggle svg{width:18px;height:18px;display:none;stroke:currentColor;fill:none;
  stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
:root:not([data-theme="dark"]) .ic-moon{display:inline}
[data-theme="dark"] .ic-sun{display:inline}

/* ---- masthead ---- */
.hero{max-width:820px;padding:44px 0 34px}
.hero h1{font-family:var(--serif);font-weight:500;font-size:2.75rem;line-height:1.08;
  letter-spacing:-0.01em;color:var(--ink)}
.hero p{margin-top:16px;font-size:1.0625rem;line-height:1.55;color:var(--muted);max-width:640px}
.trust-band{display:grid;grid-template-columns:repeat(3,1fr);
  border-top:1px solid var(--border);border-bottom:1px solid var(--border)}
.trust-band>div{padding:16px 20px 16px 0;border-right:1px solid var(--border)}
.trust-band>div:last-child{border-right:0}
.trust-band>div+div{padding-left:20px}
.kicker{font-family:var(--mono);font-size:0.875rem;font-weight:600;
  letter-spacing:.05em;color:var(--green-text);display:block;margin-bottom:6px}
.trust-band p{font-size:0.84375rem;color:var(--muted)}

/* ---- search + layout ---- */
.searchbox{position:relative;margin:26px 0 30px}
.searchbox input{width:100%;padding:16px 18px 16px 46px;font:0.9375rem var(--mono);
  color:var(--ink);background:var(--surface);border:1px solid var(--border-strong);
  border-radius:2px}
.searchbox .glyph{position:absolute;left:18px;top:50%;transform:translateY(-50%);
  color:var(--faint);pointer-events:none;font-size:1rem}
.body-grid{display:grid;grid-template-columns:248px 1fr;gap:38px;align-items:start;
  padding-bottom:60px}
@media(max-width:860px){.body-grid{grid-template-columns:1fr}
  .sidebar{position:static!important;max-height:none!important}
  .hero h1{font-size:2rem}
  /* the hero paragraph already carries the trust claim; the band's
     stacked form costs screens of scroll before search on phones */
  .trust-band{display:none}}

/* ---- sidebar facets ---- */
.sidebar{position:sticky;top:18px;max-height:calc(100vh - 36px);overflow-y:auto;
  padding:4px;margin:-4px}
.facet-group{margin-bottom:26px}
fieldset.facet-group{border:0;padding:0;min-width:0}
legend.facet-label{padding:0}
.facet-h{font-family:var(--mono);font-size:0.8125rem;font-weight:600;
  letter-spacing:.04em;color:var(--muted);
  margin:0 0 16px}
.facet-label{font-family:var(--mono);font-size:0.8125rem;
  letter-spacing:.04em;color:var(--faint-label);margin-bottom:8px}
.facet-list{display:flex;flex-direction:column;gap:1px}
.facet{display:flex;justify-content:space-between;align-items:center;width:100%;
  padding:7px 10px;border:0;border-radius:2px;background:transparent;cursor:pointer;
  font:0.84375rem var(--sans);color:var(--ink);text-align:left;gap:8px}
.facet .n{font-family:var(--mono);font-size:0.75rem;color:var(--text-subtle)}
.facet.active{background:var(--accent-soft);color:var(--accent);font-weight:500;
  box-shadow:inset 3px 0 0 0 currentColor}
.facet.active .n{color:var(--accent)}
.facet .dot{display:inline-block;width:7px;height:7px;border-radius:50%;
  margin-right:6px;vertical-align:1px}
.facet-more{background:none;border:0;cursor:pointer;font:0.78125rem var(--mono);
  color:var(--accent);padding:7px 10px;text-align:left}

/* ---- results ---- */
.results-head{display:flex;justify-content:space-between;align-items:center;
  flex-wrap:wrap;gap:10px}
.results-count{font-size:0.875rem;color:var(--muted)}
.sorts{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.sorts .lbl{font-family:var(--mono);font-size:0.8125rem;letter-spacing:.04em;
  color:var(--faint-label);margin-right:4px}
.sortbtn{background:transparent;border:1px solid transparent;border-radius:2px;
  cursor:pointer;font:0.8125rem var(--mono);color:var(--muted);padding:5px 10px;
  min-height:24px}
.sortbtn.active{border-color:var(--border-strong);background:var(--surface);color:var(--ink);
  box-shadow:inset 0 -2px 0 0 currentColor}
.sortbtn.outline{border-color:var(--border-strong);background:var(--surface);color:var(--ink)}
.chips{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:14px}
.chip{display:inline-flex;align-items:center;gap:7px;min-height:24px;
  padding:5px 10px 5px 12px;
  background:var(--surface);border:1px solid var(--border-strong);border-radius:2px;
  font:0.8125rem var(--mono);color:var(--ink);cursor:pointer}
.chip .f{color:var(--text-subtle)}
.chip .x{color:var(--text-subtle)}
.chip:hover .x{color:var(--red)}
.clear-all{background:none;border:0;cursor:pointer;font:0.8125rem var(--mono);
  color:var(--accent);padding:6px 8px;margin:-6px -8px}
.rows{margin-top:6px}
.row-item{display:flex;gap:16px;padding:20px 6px;border-bottom:1px solid var(--border);
  cursor:pointer;color:inherit;transition:background .12s;align-items:flex-start;
  content-visibility:auto;contain-intrinsic-size:1px 118px}
.row-item:hover{background:var(--surface);color:inherit}
.row-main{flex:1;min-width:0}
.row-line1{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.row-name{font-family:var(--mono);font-weight:500;font-size:0.9375rem;color:var(--ink)}
.vpill{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);
  font-size:0.75rem;color:var(--green-text)}
.vpill .c{width:15px;height:15px;border-radius:50%;background:var(--ok-fill);color:#fff;
  font-size:0.625rem;line-height:1;display:inline-grid;place-items:center;flex:none}
.row-summary{margin-top:5px;font-size:0.84375rem;line-height:1.5;color:var(--muted);
  max-width:620px;text-wrap:pretty}
.row-meta{display:flex;align-items:center;gap:14px;margin-top:8px;
  font-family:var(--mono);font-size:0.78125rem;color:var(--faint-label);flex-wrap:wrap}
.hdot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;
  vertical-align:1px}
.row-rail{display:flex;flex-direction:column;align-items:flex-end;justify-content:center;gap:8px;flex:none;
  padding-top:2px}
.row-cost{font-family:var(--mono);font-size:0.75rem;color:var(--text-secondary);
  border:1px solid var(--border-strong);border-radius:999px;padding:2px 10px}
.empty{text-align:center;padding:70px 0;font-family:var(--mono);font-size:0.875rem;
  color:var(--muted)}
.empty button{display:block;margin:16px auto 0}

/* ---- tier badges ---- */
.tb{font-family:var(--mono);font-size:0.75rem;letter-spacing:.06em;padding:2px 8px;
  border-radius:2px;white-space:nowrap}
.tb-0{background:transparent;color:var(--text-subtle);border:1px solid var(--border)}
.tb-1{background:var(--surface);color:var(--ink);border:1px solid var(--border-strong)}
.tb-2{background:transparent;color:var(--green-text);border:1px solid var(--green)}
.tb-3{background:var(--ok-fill);color:#fff;font-weight:500;border:1px solid var(--ok-fill)}

/* ---- footer ---- */
footer{border-top:1px solid var(--border);margin-top:40px;padding:18px 0 40px;
  font-family:var(--mono);font-size:0.875rem;color:var(--text-subtle);
  display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap}
footer .fine{font-size:0.75rem;align-self:flex-end}
footer .build{display:block;margin-top:4px}

/* ---- plugin detail page ---- */
.detail{max-width:860px;margin:0 auto;padding:34px 32px 90px}
.backlink{font-family:var(--mono);font-size:0.8125rem;color:var(--muted)}
.detail .crumb{font-family:var(--mono);font-size:0.8125rem;
  letter-spacing:.04em;color:var(--faint-label);margin:26px 0 6px}
.detail h1{font-family:var(--mono);font-weight:600;font-size:1.625rem;color:var(--ink)}
.strip{display:flex;align-items:center;gap:14px;margin-top:14px;flex-wrap:wrap;
  font-family:var(--mono);font-size:0.78125rem;color:var(--faint-label)}
.dsummary{margin-top:14px;font-size:0.96875rem;line-height:1.6;color:var(--text);
  max-width:660px}
.attrib{font-size:0.78125rem;color:var(--text-subtle);margin-top:8px;max-width:660px}
.sect{font-family:var(--mono);font-size:0.8125rem;font-weight:400;
  letter-spacing:.04em;color:var(--faint-label);margin:36px 0 10px}

/* install card */
.install-card{border:1px solid var(--border);border-radius:6px;
  background:var(--surface);padding:20px 24px;margin-top:28px;
  display:flex;justify-content:space-between;gap:28px;flex-wrap:wrap}
.install-card .left{flex:1;min-width:260px}
.install-card .right{display:flex;flex-direction:column;gap:8px;min-width:230px}
.inst-head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.inst-ver{font:600 1.5rem var(--mono);color:var(--ink)}
.inst-for{font-size:0.78125rem;color:var(--muted)}
.install-card select{padding:6px 26px 6px 9px;font:600 0.78125rem var(--mono);
  color:var(--ink);background:var(--bg);border:1px solid var(--accent);
  border-radius:2px;cursor:pointer}
.vline{display:flex;align-items:center;gap:8px;font-weight:600;font-size:0.84375rem;
  margin-top:12px;color:var(--green-text)}
.vline .c{width:17px;height:17px;border-radius:50%;background:var(--ok-fill);
  color:#fff;font-size:0.6875rem;line-height:1;display:inline-grid;place-items:center;flex:none}
.vline.warn{color:var(--muted)}
.vline.warn .c{background:var(--muted);color:var(--bg)}
.vdetail{margin-top:7px;font-size:0.75rem;line-height:1.5;color:var(--muted);
  max-width:520px}
.vdetail summary{cursor:pointer;font-family:var(--mono);font-size:0.75rem;
  color:var(--faint-label)}
.vdetail .hash{font-family:var(--mono);font-size:0.75rem;word-break:break-all;
  color:var(--faint-label);margin-top:5px}
.inst-meta{margin-top:10px;font-family:var(--mono);font-size:0.78125rem;
  color:var(--faint-label)}
.inst-meta b{color:var(--text);font-weight:600}
.pick-note{margin-top:10px;font-size:0.78125rem;line-height:1.5;color:var(--warn-text);
  max-width:520px}
.cmdline{display:flex;align-items:center;gap:10px;margin-top:16px;
  background:var(--ink);color:var(--bg);border-radius:3px;padding:9px 12px;
  font:0.78125rem var(--mono);max-width:520px}
.cmdline code{flex:1;overflow-x:auto;white-space:nowrap}
.cmdline button{flex:none;background:var(--bg);color:var(--ink);border:0;
  border-radius:2px;padding:6px 10px;font:600 0.75rem var(--mono);cursor:pointer;
  min-height:24px}
.ledger{position:relative;padding-left:26px;margin-top:10px}
.ledger::before{content:"";position:absolute;left:8px;top:10px;bottom:10px;
  width:1px;background:var(--border)}
.lstep{position:relative;padding:7px 0}
.lstep::before{content:"✓" / "";position:absolute;left:-26px;top:9px;width:17px;
  height:17px;border-radius:50%;background:var(--ok-fill);color:#fff;font-size:0.6875rem;
  line-height:1;display:inline-grid;place-items:center;font-weight:700}
.lstep.planned::before{content:"○" / "";background:var(--bg);color:var(--faint);
  border:1px solid var(--border-strong)}
.lstep h3{font-size:0.78125rem;font-weight:600;color:var(--text)}
.lstep.planned h3,.lstep.planned p{color:var(--text-subtle)}
.lstep p{font-family:var(--mono);font-size:0.75rem;color:var(--muted);margin-top:2px;
  word-break:break-all}
.lnote{margin-top:10px;padding-top:9px;border-top:1px dashed var(--border);
  font-size:0.8125rem;color:var(--muted)}
.cc-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chk-ok{color:var(--ok-text)}
.chk-warn{color:var(--warn-text)}
.chk-bad{color:var(--bad-text)}
.chk-muted{color:var(--text-subtle)}
.cchip{display:inline-flex;font-family:var(--mono);font-size:0.75rem;
  border-radius:2px;overflow:hidden;border:1px solid var(--border-strong)}
.cchip .l{background:var(--surface);color:var(--muted);padding:2px 7px}
.cchip .r{padding:2px 7px;font-weight:600;color:#fff}
.cchip .r.ok{background:var(--ok-fill)}
.cchip .r.bad{background:var(--bad-fill)}
.cchip .r.warn{background:var(--warn-fill)}
.cchip .r.dim{background:var(--bg);color:var(--text-secondary);font-weight:500}
.kvrow .fv .mt-name{font-size:0.9375rem;font-weight:700;color:var(--ink)}
.kvrow .fv .mt-sub{font-size:0.78125rem;color:var(--muted);margin-top:2px}
.btn{display:block;text-align:center;padding:11px 16px;border-radius:2px;
  font:500 0.8125rem var(--mono);cursor:pointer}
.act-primary{background:var(--ink);color:var(--bg);border:1px solid var(--ink)}
.act-primary:hover{color:var(--bg);opacity:.88}
.act-secondary{background:var(--bg);color:var(--muted);
  border:1px solid var(--border-strong)}

/* versions table */
.vtable{margin-top:8px}
.vrows{list-style:none;margin:0;padding:0}
.vrow{display:grid;grid-template-columns:1fr 44px;gap:12px;
  padding:10px 8px;border-bottom:1px solid var(--border);font-size:0.8125rem;
  align-items:baseline;border-radius:2px}
.vsel{display:grid;grid-template-columns:86px 130px 1fr 1fr;gap:12px;
  align-items:baseline;width:100%;background:none;border:0;padding:0;
  font:inherit;color:inherit;text-align:left;cursor:pointer}
.vrow.revoked{grid-template-columns:86px 130px 1fr 1fr 44px}
.vrow .v::before{content:"" / "";display:inline-block;width:15px}
.vrow.sel .v::before{content:"✓" / ""}
.vhead span:first-child{padding-left:15px}
.vm{display:none}
.vrow:hover{background:var(--surface)}
.vrow.sel{background:var(--accent-soft)}
.vrow .v{font-family:var(--mono);font-weight:600;color:var(--ink)}
#rev-line .msbadge{height:18px;vertical-align:-5px}
#rev-line .abadge{vertical-align:-3px}
.rev-when{color:var(--muted)}
.vhead{font-size:0.75rem;letter-spacing:.08em;text-transform:uppercase;
  color:var(--faint-label);font-family:var(--mono);
  grid-template-columns:86px 130px 1fr 1fr 44px;
  border-bottom:1px solid var(--border)}
.vhead:hover{background:transparent}
.vhead span{font-weight:400}
.qmark{display:inline-grid;place-items:center;width:17px;height:17px;
  border:1px solid var(--border-strong);border-radius:50%;
  font:600 0.75rem var(--mono);color:var(--muted);vertical-align:1px}
.qmark:hover{color:var(--ink);border-color:var(--muted)}
.qmark{position:relative}
.qmark::after{content:"";position:absolute;inset:-5px}
.vrow.sel .v{color:var(--accent)}
.vrow .d{color:var(--muted)}
.vrow .chk{font-family:var(--mono);font-size:0.78125rem}
.vrow .zl{font-family:var(--mono);font-size:0.75rem;text-align:right;
  padding:7px 8px;margin:-7px -8px}
.vrow.revoked{opacity:.6}
.vrow.revoked .v{text-decoration:line-through}
.vrow.revoked:hover{background:transparent}
.vmore{border-top:1px solid var(--border)}
.vmore summary{cursor:pointer;padding:11px 10px;font-family:var(--mono);
  font-size:0.8125rem;color:var(--muted);list-style:none;user-select:none}
.vmore summary::-webkit-details-marker{display:none}
.vmore summary::before{content:"▸ " / ""}
.vmore[open] summary::before{content:"▾ " / ""}
.vmore summary:hover{color:var(--ink)}
@media(max-width:640px){
  .vsel{grid-template-columns:70px 1fr}
  .vrow.revoked,.vhead{grid-template-columns:70px 1fr 44px}
  .vrow .chk,.vrow .rng{display:none}
  /* the hidden columns' information resurfaces inside the control (1.4.10) */
  .vsel .vm,.vrow.revoked .vm{display:block;grid-column:1/-1;
    font-family:var(--mono);font-size:0.78125rem;color:var(--muted)}}

/* screenshots */
.shots{margin-top:26px;max-width:620px}
.shot-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.shot-grid img{width:100%;aspect-ratio:16/10;object-fit:cover;object-position:top;
  border:1px solid var(--border);border-radius:3px;display:block;
  background:var(--surface)}
.shot-grid a:hover img{border-color:var(--muted)}
.shot-grid.shots-single{grid-template-columns:minmax(0,380px)}
/* lightbox (js-built; anchors fall back to the raw image without js) */
.lb{position:fixed;inset:0;background:rgba(12,11,9,.93);z-index:60;display:flex;
  flex-direction:column;align-items:center;justify-content:center;padding:24px}
.lb img{max-width:min(1200px,92vw);max-height:76vh;object-fit:contain;border-radius:4px}
.lb .lb-cap{margin-top:14px;font-size:0.84375rem;color:#e8e4dd;text-align:center;max-width:82vw}
.lb .lb-count{font-family:var(--mono);font-size:0.75rem;color:#9b968d;margin-top:6px}
.lb button{position:absolute;background:none;border:0;color:#c9c4bb;cursor:pointer;
  font:600 2.125rem var(--mono);padding:14px 18px;line-height:1}
.lb button:hover{color:#fff}
.lb .lb-x{top:8px;right:10px}
.lb .lb-prev{left:0;top:50%;transform:translateY(-50%)}
.lb .lb-next{right:0;top:50%;transform:translateY(-50%)}

/* external-link marker: these anchors open a new tab and leave
   camp-registry.org. A mask-drawn arrow keeps rendering identical in every
   font context (the self-hosted subsets lack U+2197). Direct-child scope
   keeps badge chips and buttons clean. */
.kvrow .fv > a[href^="https://"]::after,
.prose a[href^="https://"]::after,
.attrib a[href^="https://"]::after{content:"";display:inline-block;
  width:.58em;height:.58em;margin-left:.3em;vertical-align:.05em;
  background:currentColor;opacity:.7;
  -webkit-mask:url('data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12"><path d="M4 1h7v7h-2V4.4L3.4 10 2 8.6 7.6 3H4z"/></svg>') center/contain no-repeat;
  mask:url('data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12"><path d="M4 1h7v7h-2V4.4L3.4 10 2 8.6 7.6 3H4z"/></svg>') center/contain no-repeat}

/* prose / banners / advisories */
.prose{font-size:0.90625rem;line-height:1.65;color:var(--text);max-width:660px}
.prose p{margin:10px 0}
.prose h1,.prose h2,.prose h3{font-family:var(--serif);color:var(--ink);margin:18px 0 8px}
.prose code{font-family:var(--mono);font-size:.9em;background:var(--surface);
  padding:1px 5px;border-radius:2px;border:1px solid var(--border)}
.prose ul,.prose ol{padding-left:22px}
.banner{border-left:3px solid var(--amber);background:var(--surface);
  border-radius:0 3px 3px 0;padding:14px 18px;margin-top:22px;font-size:0.84375rem;
  line-height:1.55;color:var(--text)}
.adv{border:1px solid var(--green-border);background:var(--green-bg);border-radius:3px;
  padding:12px 16px;font-size:0.84375rem;color:var(--green-body);margin-bottom:8px}
.adv.open{border-color:var(--amber);background:var(--surface);color:var(--text)}
.adv .id{font-family:var(--mono);font-size:0.75rem;color:var(--faint-label);display:block;
  margin-top:4px}

.attrib a,.banner a,.detail p a,.pick-note a,footer a,noscript a,
.results-count a{text-decoration:underline;text-decoration-thickness:.1em;
  text-underline-offset:.15em}

/* project facts: one full-width row per field */
.kv{margin-top:8px}
.kvrow{display:flex;gap:26px;padding:13px 0;border-bottom:1px solid var(--border);
  align-items:baseline}
.kvrow:last-child{border-bottom:0}
.kvrow .fk{font-family:var(--mono);font-size:0.8125rem;
  letter-spacing:.04em;color:var(--text-subtle);flex:none;width:170px}
.kvrow .fv{font-size:0.84375rem;color:var(--text);flex:1}
.abadges{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.abadge{display:inline-flex;font-family:var(--mono);font-size:0.75rem;border-radius:2px;
  overflow:hidden;border:1px solid var(--border-strong);color:inherit}
.abadge .l{background:var(--ink);color:var(--bg);padding:3px 8px}
.abadge .m{padding:3px 8px;font-weight:600}
.health-line{margin-top:12px;font-family:var(--mono);font-size:0.8125rem;
  color:var(--muted)}
.labels{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.lbl-pill{font-family:var(--mono);font-size:0.78125rem;letter-spacing:.02em;
  padding:4px 13px;border-radius:999px;white-space:nowrap;
  border:1px solid var(--border-strong);background:var(--surface);
  color:var(--text)}
.msbadge{height:20px;display:inline-block;vertical-align:middle}
.msbadge-link{line-height:0;display:inline-flex;gap:4px;align-items:center}
.rev-item{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:6px}
.rev-item:first-child{margin-top:0}
.hdot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;
  vertical-align:1px}
.tb{font-family:var(--mono);font-size:0.75rem;letter-spacing:.06em;padding:2px 8px;
  border-radius:2px;white-space:nowrap}
.tb-0{background:transparent;color:var(--text-subtle);border:1px solid var(--border)}
.tb-1{background:var(--surface);color:var(--ink);border:1px solid var(--border-strong)}
.tb-2{background:transparent;color:var(--green-text);border:1px solid var(--green)}
.tb-3{background:var(--ok-fill);color:#fff;font-weight:500;border:1px solid var(--ok-fill)}

/* ---- how page ---- */
.how h1{font-family:var(--serif);font-weight:600;font-size:2.5rem;line-height:1.1;
  color:var(--ink);margin-top:22px}
.how .lead{margin-top:14px;font-size:1.0625rem;line-height:1.55;color:var(--muted);max-width:660px}
.cards3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:34px}
@media(max-width:860px){.cards3{grid-template-columns:1fr}.how h1{font-size:1.875rem}}
.tcard{border:1px solid var(--border);border-radius:6px;padding:20px 22px;
  background:var(--surface)}
.tcard p{margin-top:8px;font-size:0.875rem;color:var(--text)}
.how h2{font-family:var(--serif);font-weight:600;font-size:1.5rem;color:var(--ink);
  margin:44px 0 16px}
.step{display:flex;gap:18px;border:1px solid var(--border);border-radius:6px;
  padding:20px 22px;background:var(--surface);margin-bottom:10px}
.step .num{width:30px;height:30px;border-radius:50%;background:var(--ink);
  color:var(--bg);font:0.8125rem var(--mono);display:inline-grid;
  place-items:center;flex:none}
.step h3{font-size:1rem;font-weight:600;color:var(--ink)}
.step p{margin-top:4px;font-size:0.875rem;color:var(--muted)}
.tiergrid{display:grid;grid-template-columns:1fr;gap:10px;margin-top:16px}
.tmini{display:grid;grid-template-columns:200px 1fr;gap:6px 20px;align-items:start;
  min-width:0;border:1px solid var(--border);border-radius:5px;padding:14px 16px;background:var(--bg)}
@media(max-width:640px){.tmini{grid-template-columns:1fr}}
.facet-help{display:inline-block;margin-top:8px;font-size:0.75rem;
  color:var(--muted);text-decoration:underline;text-decoration-thickness:.1em;
  text-underline-offset:.15em}
.hterm{font-family:var(--mono);font-size:0.8125rem;font-weight:600;
  white-space:nowrap}
.tb-art{display:inline-block;line-height:0;max-width:100%}
.tb-art svg{max-width:100%;height:auto}
.tmini p{margin:0;font-size:0.8125rem;color:var(--muted);line-height:1.5}
.bigcard{border:1px solid var(--border);border-radius:6px;padding:26px 28px;
  background:var(--surface);margin-top:10px}
.cta{margin-top:44px}
.cta a{display:inline-block;margin-top:14px;padding:12px 22px;border-radius:2px;
  background:var(--ink);color:var(--bg);font:500 0.8125rem var(--mono)}
.cta a:hover{color:var(--bg);opacity:.88}

/* ---- responsive ------------------------------------------------------- */
/* Long mono strings may break anywhere rather than overflow; acts only on
   overflow so wide layouts are unchanged. (.cmdline code excluded — it
   scrolls by design.) */
.mono,.row-name,.detail h1,.detail .crumb{overflow-wrap:anywhere}
.filters-toggle{display:none}

@media(max-width:860px){
  /* iOS zooms the page on focus when a control's font is under 16px */
  .searchbox input{font-size:1rem}
  .install-card select{font-size:1rem}
  /* touch targets on the most-used controls; desktop keeps its density */
  nav a{padding:8px 0}
  .theme-toggle{padding:10px;margin:-10px}
  .sortbtn{padding:9px 12px}
  .facet,.facet-more{padding:11px 10px}
  .chip{padding:8px 10px 8px 12px}
  .cmdline button{padding:8px 12px}
  .vmore summary{padding:14px 10px}
  .vdetail summary{padding:6px 0}
  /* results come first on phones: facets collapse behind a toggle (the
     active-filter chips stay visible in the results column) */
  .filters-toggle{display:block;width:100%;text-align:left;
    background:none;border:1px solid var(--border);border-radius:4px;
    padding:12px 14px;margin-top:14px;cursor:pointer;
    font:600 0.8125rem var(--mono);color:var(--muted)}
  .filters-toggle::before{content:"▸ " / ""}
  .filters-toggle.open::before{content:"▾ " / ""}
  .filters-toggle:hover{color:var(--ink)}
  .sidebar{display:none}
  .body-grid.filters-open .sidebar{display:block}
}

@media(max-width:640px){
  /* the fixed 170px fact label (and the Maintainer row's flex:none link)
     can't share a phone-width row with its value — stack each row */
  .kvrow{flex-direction:column;align-items:flex-start;gap:6px}
  .kvrow .fk{width:auto}
}

@media(max-width:480px){
  .wrap{padding:0 18px}
  .narrow,.detail{padding:28px 18px 70px}
  .wordmark small{display:none}
  nav{gap:2px 14px}
  .nav-xtra{display:none}
  .hero h1{font-size:1.875rem}
  .detail h1{font-size:1.375rem}
  .install-card{padding:16px;gap:18px}
  .install-card .left,.install-card .right{min-width:0}
  .install-card .right{width:100%}
  .shot-grid{grid-template-columns:repeat(2,1fr)}
  .bigcard{padding:20px 18px}
  .how h1{font-size:1.6875rem}
  .narrow ul{padding-left:0}
}

/* ---- user display preferences ---- */
@media (forced-colors: active){
  .facet.active,.sortbtn.active[data-sort]{border:2px solid Highlight}
  .vrow.sel{outline:2px solid Highlight;outline-offset:-2px}
  .vsel[aria-pressed="true"]{text-decoration:underline}
  /* the dot is informative colour paired with text; keep its colour */
  .hdot{forced-color-adjust:none}
}
@media (prefers-reduced-motion: reduce){
  body,.row-item{transition:none}
}
"""

# ---------------------------------------------------------------- js -------

THEME_JS = """
(function(){
  var saved = null;
  try { saved = localStorage.getItem('camp-theme'); } catch(e){}
  var dark = saved ? saved === 'dark'
    : window.matchMedia('(prefers-color-scheme: dark)').matches;
  function apply(){
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
    // The control names the action it performs, in every path that can
    // change the theme — including the OS-preference listener below.
    var b = document.getElementById('theme-toggle');
    if (b) b.setAttribute('aria-label', dark ? 'Use light theme' : 'Use dark theme');
  }
  apply();
  // Follow live OS theme changes — but only until the user has expressed
  // a preference with the toggle; an explicit choice always wins.
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change',
    function(e){
      if (!saved){ dark = e.matches; apply(); }
    });
  document.addEventListener('DOMContentLoaded', function(){
    apply();   // the button exists now; set its initial action name
    var b = document.getElementById('theme-toggle');
    if (b) b.addEventListener('click', function(){
      dark = !dark;
      saved = dark ? 'dark' : 'light';
      try { localStorage.setItem('camp-theme', saved); } catch(e){}
      apply();
    });
  });
})();
"""

BROWSE_JS = """
(function(){
  var VORDER = %(vorder)s;
  var CHUNK = 200;
  var HEALTH = {0:['Archived upstream','var(--text-subtle)'],
    1:['Actively maintained','var(--ok-text)'], 2:['Maintained','var(--ok-text)'],
    3:['Slowing down','var(--warn-text)'], 4:['Dormant','var(--bad-text)']};
  var COST = {'paid-service':'Paid service', 'freemium':'Freemium'};
  var TIERS = ['Discovered','Claimed','Verified','Reviewed'];

  var state = {q:'', group:'', ver:'', tier:'', cost:'', health:'', sort:'relevance'};
  var shown = CHUNK;
  var data = null;
  var list = document.getElementById('rows');
  var q = document.getElementById('q');
  var countEl = document.getElementById('count');
  var chipsEl = document.getElementById('chips');
  var emptyEl = document.getElementById('empty');
  var moreBtn = document.getElementById('show-more');

  function relTime(iso){
    if (!iso) return '';
    var days = Math.floor((Date.now() - Date.parse(iso)) / 86400000);
    if (days <= 0) return 'today';
    if (days < 14) return days + ' d ago';
    if (days < 70) return Math.floor(days / 7) + ' wk ago';
    if (days < 720) return Math.floor(days / 30) + ' mo ago';
    return Math.floor(days / 365) + ' yr ago';
  }

  function restore(){
    // URL params are canonical (shareable links); within a tab session,
    // returning via any plain link to "/" (Back to archive, Browse)
    // recovers the last filters from sessionStorage.
    var qs = location.search.slice(1);
    if (!qs){
      try { qs = sessionStorage.getItem('camp-browse') || ''; } catch(e){}
    }
    var p = new URLSearchParams(qs);
    ['q','group','ver','tier','cost','health','sort'].forEach(function(k){
      if (p.get(k)) state[k] = p.get(k);
    });
    q.value = state.q;
  }
  function persist(){
    var p = new URLSearchParams();
    ['q','group','ver','tier','cost','health'].forEach(function(k){
      if (state[k]) p.set(k, state[k]);
    });
    if (state.sort !== 'relevance') p.set('sort', state.sort);
    var qs = p.toString();
    history.replaceState(null, '', qs ? '?' + qs : location.pathname);
    try { sessionStorage.setItem('camp-browse', qs); } catch(e){}
  }

  function passes(o){
    // Per-filter flags, so facet counts can exclude one dimension at a time.
    return {
      q: !state.q || o.blob.indexOf(state.q) !== -1,
      group: !state.group || o.g === state.group,
      ver: !state.ver || (o.a >= 0 && (function(){
        var i = VORDER.indexOf(state.ver); return i >= o.a && i <= o.b; })()),
      tier: state.tier === '' || o.t === +state.tier,
      cost: !state.cost || o.l.indexOf(state.cost) !== -1,
      health: state.health === '' || o.h === +state.health
    };
  }
  function allPass(f){
    return f.q && f.group && f.ver && f.tier && f.cost && f.health;
  }

  function cmp(a, b){ return a < b ? -1 : a > b ? 1 : 0; }
  var SORTS = {
    relevance: function(a,b){ return cmp(b.t,a.t) || cmp(b.s,a.s) || cmp(a.c,b.c); },
    stars: function(a,b){ return b.s - a.s || cmp(a.c, b.c); },
    recent: function(a,b){ return cmp(b.u || '', a.u || ''); },
    az: function(a,b){ return cmp(a.c, b.c); }
  };

  function el(tag, cls, text){
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function rowNode(o){
    var a = el('a', 'row-item');
    a.href = '/plugin/' + o.c + '.html';
    var main = el('div', 'row-main');
    var l1 = el('div', 'row-line1');
    l1.appendChild(el('span', 'row-name', o.c));
    var tb = el('span', 'tb tb-' + o.t, 'Tier ' + o.t + ' \u00b7 ' + TIERS[o.t]);
    l1.appendChild(tb);
    if (o.t >= 2 && o.p){
      var vp = el('span', 'vpill');
      var vc = el('span', 'c', '\u2713');
      vc.setAttribute('aria-hidden', 'true');
      vp.appendChild(vc);
      vp.appendChild(document.createTextNode('verified ' + relTime(o.p)));
      l1.appendChild(vp);
    }
    main.appendChild(l1);
    if (o.m) main.appendChild(el('div', 'row-summary', o.m));
    var meta = el('div', 'row-meta');
    if (HEALTH[o.h]){
      var hspan = el('span', null);
      hspan.style.color = HEALTH[o.h][1];
      var dot = el('span', 'hdot'); dot.style.background = HEALTH[o.h][1];
      dot.setAttribute('aria-hidden', 'true');
      hspan.appendChild(dot);
      hspan.appendChild(document.createTextNode(HEALTH[o.h][0]));
      meta.appendChild(hspan);
    }
    if (o.u) meta.appendChild(el('span', null, 'updated ' + relTime(o.u)));
    if (o.h !== -1){
      var stars = el('span', null);
      stars.setAttribute('aria-label', o.s + ' GitHub stars, ' + o.f +
        ' forks, ' + o.o + ' open issues and pull requests');
      var glyph = el('span', null, '\u2605');
      glyph.setAttribute('aria-hidden', 'true');
      stars.appendChild(glyph);
      stars.appendChild(document.createTextNode(' ' + o.s + ' \u00b7 ' + o.f +
        ' forks \u00b7 ' + o.o + ' open issues & PRs'));
      meta.appendChild(stars);
    }
    main.appendChild(meta);
    a.appendChild(main);
    var cost = (o.l || []).map(function(k){ return COST[k]; }).filter(Boolean)[0];
    if (cost){
      var rail = el('div', 'row-rail');
      rail.appendChild(el('span', 'row-cost', cost));
      a.appendChild(rail);
    }
    return a;
  }

  var facets = [].slice.call(document.querySelectorAll('.facet')).map(function(f){
    return {el:f, g:f.dataset.facet, v:f.dataset.value, n:f.querySelector('.n')};
  });

  // The single place toggle state is rendered: the class and the ARIA
  // state can never diverge, whatever path (click, restore, clear) led here.
  function setPressed(el, on){
    el.classList.toggle('active', on);
    el.setAttribute('aria-pressed', String(on));
  }

  function apply(){
    if (!data){ persist(); return; }   // still loading; state applies on arrival
    var matched = [];
    var counts = {};                    // one pass over the data for everything
    facets.forEach(function(f){ counts[f.g + '|' + f.v] = 0; });
    for (var k = 0; k < data.length; k++){
      var o = data[k], fl = passes(o);
      if (allPass(fl)) matched.push(o);
      for (var j = 0; j < facets.length; j++){
        var f = facets[j];
        var others = (f.g !== 'q' ? fl.q : true)
          && (f.g === 'group' ? true : fl.group)
          && (f.g === 'ver' ? true : fl.ver)
          && (f.g === 'tier' ? true : fl.tier)
          && (f.g === 'cost' ? true : fl.cost)
          && (f.g === 'health' ? true : fl.health);
        if (!others) continue;
        var hit;
        if (!f.v) hit = true;
        else if (f.g === 'group') hit = o.g === f.v;
        else if (f.g === 'ver'){
          var i = VORDER.indexOf(f.v);
          hit = o.a >= 0 && i >= o.a && i <= o.b;
        }
        else if (f.g === 'tier') hit = o.t === +f.v;
        else if (f.g === 'health') hit = o.h === +f.v;
        else hit = o.l.indexOf(f.v) !== -1;
        if (hit) counts[f.g + '|' + f.v]++;
      }
    }
    matched.sort(SORTS[state.sort] || SORTS.relevance);

    list.textContent = '';
    var frag = document.createDocumentFragment();
    matched.slice(0, shown).forEach(function(o){ frag.appendChild(rowNode(o)); });
    list.appendChild(frag);
    moreBtn.style.display = matched.length > shown ? '' : 'none';
    moreBtn.textContent = 'Show ' +
      Math.min(CHUNK, matched.length - shown) + ' more of ' + matched.length;

    var n = matched.length;
    if (!n){
      countEl.textContent = 'No plugins match the selected filters.';
    } else {
      var msg = n.toLocaleString() + ' plugin' + (n === 1 ? '' : 's') +
        (state.q ? ' match' + (n === 1 ? 'es' : '') +
          ' \u201c' + state.q + '\u201d.' : ' found.');
      if (n > shown) msg += ' First ' + shown.toLocaleString() + ' shown.';
      countEl.textContent = msg;
    }
    emptyEl.style.display = matched.length ? 'none' : '';
    facets.forEach(function(f){
      if (f.n) f.n.textContent = counts[f.g + '|' + f.v];
      setPressed(f.el, state[f.g] === f.v || (!state[f.g] && !f.v));
    });
    document.querySelectorAll('.sortbtn[data-sort]').forEach(function(b){
      setPressed(b, b.dataset.sort === state.sort);
    });
    renderChips();
    persist();
  }

  var CHIP_FIELDS = {q:'search', group:'type', ver:'moodle', tier:'tier',
    cost:'cost', health:'health'};
  function chipLabel(k){
    if (k === 'q') return '\u201c' + state.q + '\u201d';
    var f = document.querySelector(
      '.facet[data-facet="'+k+'"][data-value="'+state[k]+'"] .t');
    return f ? f.textContent : state[k];
  }
  function renderChips(){
    var any = state.q || state.group || state.ver || state.tier ||
      state.cost || state.health;
    var ftBtn = document.getElementById('filters-toggle');
    if (ftBtn){
      var n = ['group','ver','tier','cost','health']
        .filter(function(k){ return state[k]; }).length;
      ftBtn.textContent = n ? 'Filters · ' + n + ' active' : 'Filters';
    }
    chipsEl.innerHTML = '';
    chipsEl.style.display = any ? '' : 'none';
    if (!any) return;
    Object.keys(CHIP_FIELDS).forEach(function(k){
      if (!state[k]) return;
      var c = document.createElement('button');
      c.className = 'chip';
      c.setAttribute('aria-label',
        'Remove filter: ' + CHIP_FIELDS[k] + ', ' + chipLabel(k));
      c.innerHTML = '<span class="f">' + CHIP_FIELDS[k] + '</span>';
      var val = document.createElement('span');
      val.textContent = chipLabel(k);
      c.appendChild(val);
      c.insertAdjacentHTML('beforeend', ' <span class="x" aria-hidden="true">\u00d7</span>');
      c.addEventListener('click', function(){
        state[k] = ''; if (k === 'q') q.value = '';
        shown = CHUNK; apply();
        // Keep focus predictable after removal: the next chip if any
        // remain, otherwise the search field \u2014 never the result list.
        var left = chipsEl.querySelectorAll('.chip');
        if (left.length) left[0].focus(); else q.focus();
      });
      chipsEl.appendChild(c);
    });
    var clear = document.createElement('button');
    clear.className = 'clear-all'; clear.textContent = 'Clear all';
    clear.addEventListener('click', clearAll);
    chipsEl.appendChild(clear);
  }
  function clearAll(){
    state.q = state.group = state.ver = state.tier = state.cost = '';
    state.health = '';
    q.value = ''; shown = CHUNK; apply();
  }
  var filtersToggle = document.getElementById('filters-toggle');
  if (filtersToggle) filtersToggle.addEventListener('click', function(){
    var grid = document.querySelector('.body-grid');
    var open = grid.classList.toggle('filters-open');
    filtersToggle.classList.toggle('open', open);
    filtersToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  });

  var debounce = null;
  q.addEventListener('input', function(){
    clearTimeout(debounce);
    debounce = setTimeout(function(){
      state.q = q.value.trim().toLowerCase(); shown = CHUNK; apply();
    }, 250);
  });
  document.querySelectorAll('.facet').forEach(function(f){
    f.addEventListener('click', function(){
      var g = f.dataset.facet, v = f.dataset.value;
      state[g] = (state[g] === v) ? '' : v;
      shown = CHUNK; apply();
    });
  });
  document.querySelectorAll('.sortbtn').forEach(function(b){
    if (b.dataset.sort)
      b.addEventListener('click', function(){ state.sort = b.dataset.sort; apply(); });
  });
  document.querySelectorAll('.facet-more').forEach(function(btn){
    btn.addEventListener('click', function(){
      var tgt = document.getElementById(btn.dataset.target);
      var open = !tgt.hidden;
      tgt.hidden = open;
      btn.setAttribute('aria-expanded', String(!open));
      btn.textContent = open ? btn.dataset.more : btn.dataset.less;
    });
  });
  moreBtn.addEventListener('click', function(){ shown += CHUNK; apply(); });
  var clearEmpty = document.getElementById('clear-empty');
  if (clearEmpty) clearEmpty.addEventListener('click', clearAll);

  restore();
  countEl.textContent = 'Loading plugins\u2026';
  fetch('/index.json').then(function(r){ return r.json(); }).then(function(j){
    data = j.plugins.map(function(o){
      o.blob = (o.c + ' ' + (o.m || '') + ' ' + (o.n || '')).toLowerCase();
      o.l = o.l || [];
      return o;
    });
    apply();
  }).catch(function(){
    // JSON unavailable: the server-rendered first page stays; filters
    // are disabled rather than silently wrong. The count element is the
    // status region, so this is announced as well as shown.
    countEl.innerHTML = 'Showing the first rows only \u2014 the full index ' +
      'is unavailable. Browse the <a href="/all.html">complete plain index</a>.';
  });
})();
"""

COPY_JS = """
document.addEventListener('DOMContentLoaded', function(){
  var b = document.getElementById('copy-install');
  var copyStatus = document.getElementById('copy-status');
  if (b) b.addEventListener('click', function(){
    // Success is reported only when the clipboard write actually resolves;
    // failure gets a manual-copy instruction. Focus stays on the button.
    var done = function(ok){
      if (copyStatus) copyStatus.textContent = ok
        ? 'Install command copied to clipboard.'
        : 'The command could not be copied. Select and copy it manually.';
      if (ok){
        b.textContent = 'Copied';
        setTimeout(function(){ b.textContent = 'Copy'; }, 1600);
      }
    };
    if (navigator.clipboard && navigator.clipboard.writeText)
      navigator.clipboard.writeText(b.dataset.cmd)
        .then(function(){ done(true); }, function(){ done(false); });
    else done(false);
  });

  // Version picker: repoint the facts rail at the newest release that
  // supports the admin's Moodle branch. The choice persists site-wide.
  var dataEl = document.getElementById('rel-data');
  var pick = document.getElementById('vpick');
  if (!dataEl || !pick) return;
  var DATA = JSON.parse(dataEl.textContent);
  var releases = DATA.releases, VORDER = DATA.vorder;

  function vkey(v){ return v.split('.').map(function(n){ return +n || 0; }); }
  function vcmp(a, b){
    var ka = vkey(a), kb = vkey(b);
    for (var i = 0; i < Math.max(ka.length, kb.length); i++){
      var d = (ka[i] || 0) - (kb[i] || 0);
      if (d) return d;
    }
    return 0;
  }
  function bestFor(branch){
    var i = VORDER.indexOf(branch), best = null;
    releases.forEach(function(r){
      if (i >= r.lo && i <= r.hi && (!best || vcmp(r.v, best.v) > 0)) best = r;
    });
    return best;
  }
  function set(id, text){ var e = document.getElementById(id); if (e) e.textContent = text; }
  var verStatus = document.getElementById('ver-status');
  var announceReady = false;   // initial render must not produce chatter
  function fmtRange(r){
    return r.lo === r.hi ? VORDER[r.lo] : VORDER[r.lo] + ' – ' + VORDER[r.hi];
  }
  // One path renders selection state, so the class and ARIA never diverge.
  function selectRow(v){
    document.querySelectorAll('.vsel[data-ver]').forEach(function(btn){
      var on = btn.dataset.ver === v;
      btn.setAttribute('aria-pressed', String(on));
      var li = btn.closest('.vrow');
      if (li) li.classList.toggle('sel', on);
    });
  }
  function render(r, noteText){
    var note = document.getElementById('pick-note');
    if (noteText){ note.textContent = noteText; note.style.display = ''; }
    else { note.style.display = 'none'; }
    var zip = document.getElementById('zip-btn');
    if (zip) zip.href = r.zip;
    set('zip-ver', r.v);
    set('compat', r.lo === r.hi ? VORDER[r.lo] : VORDER[r.lo] + ' – ' + VORDER[r.hi]);
    set('vd-tag', r.tag); set('vd-commit', r.commit);
    set('vd-date', r.date); set('vd-sha', r.sha);
    // Composer command: pinned when the chosen version is not the newest
    // (composer would otherwise resolve to the newest regardless of the
    // site's Moodle branch).
    var newest = releases.reduce(function(a, b){ return vcmp(a.v, b.v) >= 0 ? a : b; });
    var cmd = 'composer require ' + DATA.package +
      (r.v === newest.v ? '' : ':"' + r.v + '"');
    var cmdText = document.getElementById('cmd-text');
    if (cmdText) cmdText.textContent = cmd;
    var copyBtn = document.getElementById('copy-install');
    if (copyBtn) copyBtn.dataset.cmd = cmd;
    var cc = document.getElementById('cc-text');
    if (cc){
      if (r.check){
        cc.textContent = r.check.text;
        cc.className = 'chk-' + r.check.status;
        var m = document.getElementById('cc-meta');
        if (m) m.textContent = r.check.tag;
        var box = document.getElementById('cc-chips');
        if (box){
          box.innerHTML = '';
          function chip(label, value, cls, name){
            var c = document.createElement('span'); c.className = 'cchip';
            var l = document.createElement('span'); l.className = 'l';
            l.textContent = label;
            var v = document.createElement('span'); v.className = 'r ' + cls;
            if (name){
              v.setAttribute('aria-label', name);
              var g = document.createElement('span');
              g.setAttribute('aria-hidden', 'true');
              g.textContent = value;
              v.appendChild(g);
            } else {
              v.textContent = value;
            }
            c.appendChild(l); c.appendChild(v); box.appendChild(c);
          }
          chip('phplint', r.check.phplint ? '\u2713' : '\u2717',
               r.check.phplint ? 'ok' : 'bad',
               r.check.phplint ? 'PHP lint passed' : 'PHP lint failed');
          chip('phpcs',
               (r.check.text.indexOf('errors') !== -1 ? r.check.text
                 .replace(' errors \u00b7 ', ' | ').replace(' warnings', '')
                 .replace('0 | ', '0 | ') : r.check.text),
               r.check.text.indexOf('0 errors') === 0 || r.check.text === 'clean'
                 ? 'ok' : 'bad');
          if (r.check.files) chip('files', r.check.files, 'dim');
          var groups = {};
          Object.keys(r.check.rules || {}).forEach(function(k){
            var parts = k.split('.');
            var g = parts.length >= 2 ? parts[parts.length - 2] : k;
            groups[g] = (groups[g] || 0) + r.check.rules[k];
          });
          Object.keys(groups).sort(function(a, b){ return groups[b] - groups[a]; })
            .slice(0, 4).forEach(function(g){
              chip(g, '\u00d7' + groups[g], 'warn');
            });
        }
      } else {
        cc.textContent = 'not yet checked';
        cc.className = 'chk-muted';
        var m2 = document.getElementById('cc-meta');
        if (m2) m2.textContent = r.tag;
        var box2 = document.getElementById('cc-chips');
        if (box2) box2.innerHTML = '';   // never show another version's chips
      }
    }
    var rl = document.getElementById('rev-line');
    if (rl){
      var rb = document.getElementById('rev-body');
      if (r.review_html){
        if (rb) rb.innerHTML = r.review_html;
        rl.hidden = false;
      } else {
        if (rb) rb.innerHTML = '';
        rl.hidden = true;
      }
    }
    selectRow(r.v);
    if (announceReady && verStatus){
      verStatus.textContent = noteText ||
        ('Version ' + r.v + ' selected. Compatible with Moodle ' +
         fmtRange(r) + '.');
    }
  }
  function apply(branch){
    var r = bestFor(branch);
    if (!r){
      // Nothing supports this branch: keep the newest release visible but
      // say so plainly rather than pretending.
      r = releases.reduce(function(a, b){ return vcmp(a.v, b.v) >= 0 ? a : b; });
      render(r, 'No verified release supports Moodle ' + branch +
        ' yet. Newest available is v' + r.v + ' (Moodle ' +
        VORDER[r.lo] + ' – ' + VORDER[r.hi] + ').');
      return;
    }
    render(r, null);
  }
  function applyVersion(r){
    // Explicit version choice from the history: show exactly that version,
    // and say what it supports. The picker aligns without persisting —
    // browsing a version is not declaring your Moodle.
    if (VORDER.indexOf(pick.value) < r.lo || VORDER.indexOf(pick.value) > r.hi)
      pick.value = VORDER[r.hi];
    var best = bestFor(pick.value);
    render(r, (best && best.v !== r.v)
      ? 'v' + r.v + ' supports Moodle ' + VORDER[r.lo] + ' – ' + VORDER[r.hi] +
        '. Newest for Moodle ' + pick.value + ' is v' + best.v + '.'
      : null);
  }
  document.querySelectorAll('.vsel[data-ver]').forEach(function(btn){
    btn.addEventListener('click', function(){
      var r = releases.filter(function(x){ return x.v === btn.dataset.ver; })[0];
      if (r) applyVersion(r);
    });
  });

  var saved = null;
  try { saved = localStorage.getItem('camp-moodle'); } catch(e){}
  var options = [].map.call(pick.options, function(o){ return o.value; });
  var initial = (saved && options.indexOf(saved) !== -1) ? saved : options[0];
  pick.value = initial;
  apply(initial);
  announceReady = true;
  pick.addEventListener('change', function(){
    try { localStorage.setItem('camp-moodle', pick.value); } catch(e){}
    apply(pick.value);
  });
});
"""

LIGHTBOX_JS = """
document.addEventListener('DOMContentLoaded', function(){
  // Gallery lightbox. The anchors keep real hrefs, so without JS a click
  // still opens the raw image; with JS it opens the overlay instead.
  var links = Array.prototype.slice.call(document.querySelectorAll('[data-lb]'));
  if (!links.length) return;
  links.sort(function(a, b){ return (+a.dataset.lb) - (+b.dataset.lb); });
  var shots = links.map(function(a){
    return {src: a.getAttribute('href'), cap: a.dataset.caption || ''};
  });
  var lb = null, img, capEl, countEl, idx = 0;
  function build(){
    lb = document.createElement('div');
    lb.className = 'lb';
    lb.innerHTML =
      '<button class="lb-x" aria-label="Close">\\u00d7</button>' +
      '<button class="lb-prev" aria-label="Previous">\\u2039</button>' +
      '<img alt="">' +
      '<div class="lb-cap"></div><div class="lb-count"></div>' +
      '<button class="lb-next" aria-label="Next">\\u203a</button>';
    img = lb.querySelector('img');
    capEl = lb.querySelector('.lb-cap');
    countEl = lb.querySelector('.lb-count');
    lb.querySelector('.lb-x').addEventListener('click', close);
    lb.querySelector('.lb-prev').addEventListener('click', function(e){
      e.stopPropagation(); step(-1); });
    lb.querySelector('.lb-next').addEventListener('click', function(e){
      e.stopPropagation(); step(1); });
    lb.addEventListener('click', function(e){ if (e.target === lb) close(); });
    if (shots.length < 2){
      lb.querySelector('.lb-prev').style.display = 'none';
      lb.querySelector('.lb-next').style.display = 'none';
      countEl.style.display = 'none';
    }
    var x0 = null;
    lb.addEventListener('touchstart', function(e){
      x0 = e.touches[0].clientX; }, {passive: true});
    lb.addEventListener('touchend', function(e){
      if (x0 === null) return;
      var dx = e.changedTouches[0].clientX - x0; x0 = null;
      if (Math.abs(dx) > 40) step(dx > 0 ? -1 : 1);
    }, {passive: true});
    document.body.appendChild(lb);
  }
  function show(){
    var s = shots[idx];
    img.src = s.src; img.alt = s.cap || 'screenshot';
    capEl.textContent = s.cap;
    capEl.style.display = s.cap ? '' : 'none';
    countEl.textContent = (idx + 1) + ' / ' + shots.length;
  }
  function step(d){ idx = (idx + d + shots.length) % shots.length; show(); }
  function onkey(e){
    if (e.key === 'Escape') close();
    else if (e.key === 'ArrowLeft') step(-1);
    else if (e.key === 'ArrowRight') step(1);
  }
  function open(i){
    if (!lb) build();
    idx = i; show();
    lb.style.display = 'flex';
    document.addEventListener('keydown', onkey);
  }
  function close(){
    lb.style.display = 'none';
    document.removeEventListener('keydown', onkey);
  }
  links.forEach(function(a, i){
    a.addEventListener('click', function(e){ e.preventDefault(); open(i); });
  });
});
"""

# ------------------------------------------------------------- helpers -----


from .validate import newest_release as _newest_release  # noqa: E402


def _sniff_groups(rules: dict, top: int = 4) -> list[tuple[str, int]]:
    """Aggregate phpcs rule counts up to sniff families for display."""
    groups: dict[str, int] = {}
    for src, n in (rules or {}).items():
        parts = src.split(".")
        key = parts[-2] if len(parts) >= 2 else src
        groups[key] = groups.get(key, 0) + n
    return sorted(groups.items(), key=lambda kv: -kv[1])[:top]


def _check_chips(vcheck: dict) -> str:
    """moodle.org-style chip row for one version's check summary."""
    chips = []
    ok = vcheck.get("phplint", True)
    chips.append(f'<span class="cchip"><span class="l">phplint</span>'
                 f'<span class="r {"ok" if ok else "bad"}" '
                 f'aria-label="PHP lint {"passed" if ok else "failed"}">'
                 f'<span aria-hidden="true">{"✓" if ok else "✗"}</span>'
                 f'</span></span>')
    errors, warnings = vcheck.get("errors", 0), vcheck.get("warnings", 0)
    cls = "ok" if errors == 0 else "bad"
    chips.append(f'<span class="cchip"><span class="l">phpcs</span>'
                 f'<span class="r {cls}" aria-label="{errors} errors, '
                 f'{warnings} warnings">{errors} | {warnings}</span></span>')
    if vcheck.get("files"):
        chips.append(f'<span class="cchip"><span class="l">files</span>'
                     f'<span class="r dim">{vcheck["files"]}</span></span>')
    for name, n in _sniff_groups(vcheck.get("rules")):
        chips.append(f'<span class="cchip"><span class="l">{escape(name)}</span>'
                     f'<span class="r warn">×{n}</span></span>')
    return "".join(chips)


def _tier_badge(tier: int) -> str:
    return f'<span class="tb tb-{tier}">Tier {tier} · {TIER_NAMES[tier]}</span>'


def _tier_artwork(tier: int) -> str:
    """The literal tier shield (badge.py's own renderer) inlined, for the
    how-it-works explainer — there the badge is the content being
    explained, so artwork beats an HTML chip. Gradient/clip ids are
    uniquified because three of these share the page."""
    doc = badge_mod.endpoint_document(tier)
    svg = badge_mod.render_svg(doc["label"], doc["message"], doc["color"])
    for old in ("s", "r"):
        svg = svg.replace(f'id="{old}"', f'id="{old}-t{tier}"').replace(
            f"url(#{old})", f"url(#{old}-t{tier})")
    # the standalone artifact carries no viewBox (fixed-size file); inlined
    # in a grid it must be able to scale down, so derive one from its width
    width = svg.split('width="', 1)[1].split('"', 1)[0]
    svg = svg.replace('height="20"', f'height="20" viewBox="0 0 {width} 20"', 1)
    return f'<span class="tb-art">{svg}</span>'


def _rel_time(iso: str, today: datetime.date) -> str:
    try:
        then = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).date()
    except ValueError:
        return iso
    days = (today - then).days
    if days <= 0:
        return "today"
    if days < 14:
        return f"{days} d ago"
    if days < 70:
        return f"{days // 7} wk ago"
    if days < 720:
        return f"{days // 30} mo ago"
    return f"{days // 365} yr ago"


def _health(entry: dict, today: datetime.date) -> tuple[str, str] | None:
    """(css color, label) from upstream activity, or None if unknown."""
    metrics = entry.get("metrics") or {}
    if metrics.get("archived"):
        return ("var(--text-subtle)", "Archived upstream")
    updated = metrics.get("updated")
    if not updated:
        return None
    try:
        then = datetime.datetime.fromisoformat(updated.replace("Z", "+00:00")).date()
    except ValueError:
        return None
    days = (today - then).days
    if days < 180:
        return ("var(--ok-text)", "Actively maintained")
    if days < 540:
        return ("var(--ok-text)", "Maintained")
    if days < 1095:
        return ("var(--warn-text)", "Slowing down")
    return ("var(--bad-text)", "Dormant")


LABEL_NAMES = {
    "fully-free": "Fully free",
    "freemium": "Freemium",
    "paid-service": "Paid service",
    "external-account": "External account required",
    "donation-supported": "Donation supported",
    "commercial-support-available": "Commercial support available",
}


def _cost_text(entry: dict) -> str:
    labels = entry.get("labels", [])
    for key in ("paid-service", "freemium"):
        if key in labels:
            return {"paid-service": "Paid service",
                    "freemium": "Freemium"}[key]
    return ""


def _moodle_range(release: dict) -> str:
    supported = release["supported-moodle"]
    return supported[0] if len(supported) == 1 else f"{supported[0]} – {supported[-1]}"


def _range_indices(entry: dict) -> tuple[int, int]:
    """(lo, hi) indices into VORDER for the latest release's range; (-1, -1)
    when the entry has no releases — version filters then exclude it."""
    if not entry["releases"]:
        return (-1, -1)
    supported = _newest_release(entry)["supported-moodle"]
    known = [v for v in supported if v in VORDER]
    if not known:
        return (-1, -1)
    return (VORDER.index(known[0]), VORDER.index(known[-1]))


def _type_label(plugintype: str) -> str:
    return PLUGINTYPE_NAMES.get(plugintype, plugintype)


def _zip_url(artifacts_base: str, component: str, version: str) -> str:
    return f"{artifacts_base}/{component}/{component}-{version}.zip"


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


def _fmt_date(iso: str) -> str:
    try:
        return datetime.datetime.fromisoformat(
            iso.replace("Z", "+00:00")).strftime("%d %b %Y")
    except ValueError:
        return iso


# ---------------------------------------------------------------- page -----


def _page(title: str, body: str, *, description: str = "", extra_js: str = "") -> str:
    desc = (f'<meta name="description" content="{escape(description)}">'
            if description else "")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
{desc}
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png">
<link rel="icon" type="image/png" sizes="16x16" href="/favicon-16.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<style>{CSS}</style>
<script>{THEME_JS}</script>
</head>
<body>
<a class="skip-link" href="#main-content">Skip to main content</a>
{body}
<script>{extra_js}</script>
</body>
</html>"""


def _header() -> str:
    inner = f"""
  <a class="wordmark" href="/"><b>CAMP</b>
    <small>Community Archive of Plugins for Moodle</small></a>
  <nav aria-label="Primary">
    <a href="/">Browse</a>
    <a href="/how-it-works.html">How it works</a>
    <a href="https://github.com/camp-registry/camp-docs">Docs</a>
    <a href="{MIRROR_URL}">Mirror<span class="nav-xtra"> this archive</span></a>
    <button class="theme-toggle" id="theme-toggle" aria-label="Switch theme">
      <svg class="ic-moon" viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
      <svg class="ic-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
    </button>
  </nav>"""
    # One header everywhere: same container, same border. The prototype used
    # two treatments, but its plugin detail was a slide-over — page-to-page
    # navigation makes shifting header chrome visibly inconsistent.
    return f'<header><div class="wrap"><div class="topbar">{inner}</div></div></header>'


_BUILT = datetime.datetime.now(datetime.timezone.utc)


def _footer(wrap: bool = True) -> str:
    built = _BUILT.strftime("%Y-%m-%d %H:%M UTC")
    inner = (f"""<footer>
  <span>CAMP is a community-governed archive of plugins for Moodle™.
  Open data, mirrorable by anyone.</span>
  <span class="fine">Not affiliated with or endorsed by Moodle Pty Ltd.
  <span class="build">camp-tools v{TOOLS_VERSION} · built {built}</span></span>
</footer>""")
    return f'<div class="wrap">{inner}</div>' if wrap else inner


# ------------------------------------------------------------- browse ------


def _facet(group: str, value: str, text: str, *, dot: str = "") -> str:
    dothtml = (f'<span class="dot" aria-hidden="true" style="background:{dot}"></span>'
               if dot else "")
    return (f'<button class="facet" data-facet="{group}" data-value="{escape(value)}" '
            f'aria-pressed="false">'
            f'<span>{dothtml}<span class="t">{escape(text)}</span></span>'
            f'<span class="n"></span></button>')


def _browse_page(entries: list[tuple[dict, dict]], today: datetime.date) -> str:
    total = len(entries)

    type_counts = Counter(e["component"].partition("_")[0] for e, _ in entries)
    by_count = [t for t, _ in type_counts.most_common()]
    top_types, more_types = by_count[:6], by_count[6:]

    type_facets = _facet("group", "", "All types")
    type_facets += "".join(_facet("group", t, _type_label(t)) for t in sorted(
        top_types, key=lambda t: _type_label(t).lower()))
    more_html = ""
    if more_types:
        hidden = "".join(_facet("group", t, _type_label(t)) for t in sorted(
            more_types, key=lambda t: _type_label(t).lower()))
        more_html = (f'<div id="more-types" hidden>{hidden}</div>'
                     f'<button class="facet-more" data-target="more-types" '
                     f'aria-expanded="false" aria-controls="more-types" '
                     f'data-more="+ Show all {len(more_types)} more types" '
                     f'data-less="− Show fewer types">'
                     f'+ Show all {len(more_types)} more types</button>')

    vlist = list(reversed(VORDER))
    top_vers, more_vers = vlist[:4], vlist[4:]
    ver_facets = _facet("ver", "", "Any version")
    ver_facets += "".join(_facet("ver", v, f"Moodle {v}") for v in top_vers)
    ver_more = ""
    if more_vers:
        hidden = "".join(_facet("ver", v, f"Moodle {v}") for v in more_vers)
        ver_more = (f'<div id="more-vers" hidden>{hidden}</div>'
                    f'<button class="facet-more" data-target="more-vers" '
                    f'aria-expanded="false" aria-controls="more-vers" '
                    f'data-more="+ Show {len(more_vers)} older versions" '
                    f'data-less="− Show fewer versions">'
                    f'+ Show {len(more_vers)} older versions</button>')

    tier_dots = {0: "var(--border)", 1: "var(--border-strong)",
                 2: "var(--green)", 3: "var(--green)"}
    tier_facets = _facet("tier", "", "Any tier") + "".join(
        _facet("tier", str(t), f"Tier {t} — {TIER_NAMES[t]}", dot=tier_dots[t])
        for t in sorted(TIER_NAMES))

    cost_facets = (
        _facet("cost", "", "Any cost model")
        + _facet("cost", "fully-free", "Fully free")
        + _facet("cost", "donation-supported", "Donation-supported")
        + _facet("cost", "commercial-support-available", "Commercial support"))

    # values are the same health codes the row records carry in "h"
    health_facets = (
        _facet("health", "", "Any health")
        + _facet("health", "1", "Actively maintained", dot="var(--ok-text)")
        + _facet("health", "2", "Maintained", dot="var(--ok-text)")
        + _facet("health", "3", "Slowing down", dot="var(--warn-text)")
        + _facet("health", "4", "Dormant", dot="var(--bad-text)")
        + _facet("health", "0", "Archived upstream", dot="var(--text-subtle)"))

    HEALTH_CODE = {"Archived upstream": 0, "Actively maintained": 1,
                   "Maintained": 2, "Slowing down": 3, "Dormant": 4}
    records = []
    for entry, listing in entries:
        component = entry["component"]
        metrics = entry.get("metrics") or {}
        health = _health(entry, today)
        latest = _newest_release(entry)
        vlo, vhi = _range_indices(entry)
        maints = " ".join(
            str(m.get(k, "")) for m in entry.get("maintainers", [])
            for k in ("name", "github", "gitlab") if m.get(k))
        display = listing.get("name") or ""
        rec = {"c": component, "g": component.partition("_")[0],
               "t": entry["tier"], "s": metrics.get("stars", 0),
               "f": metrics.get("forks", 0), "o": metrics.get("open-issues", 0),
               "u": metrics.get("updated", ""),
               "h": HEALTH_CODE.get(health[1], -1) if health else -1,
               "l": entry.get("labels", []),
               "m": (listing.get("summary") or entry.get("summary") or "").strip(),
               "n": f"{display} {maints}".strip().lower(),
               "a": vlo, "b": vhi}
        if entry["tier"] >= 2 and latest:
            rec["p"] = latest["published"]
        records.append(rec)

    # Server-render only the first page (default relevance order): the full
    # catalog rides /index.json and renders client-side in chunks — parsing
    # 5,700 prebuilt rows is what made the old page slow.
    by_relevance = sorted(
        enumerate(entries),
        key=lambda pair: (-pair[1][0]["tier"],
                          -(pair[1][0].get("metrics") or {}).get("stars", 0),
                          pair[1][0]["component"]))
    first_page = [pair for _, pair in by_relevance[:150]]

    rows_html = []
    for entry, listing in first_page:
        component = entry["component"]
        plugintype = component.partition("_")[0]
        summary = (listing.get("summary") or entry.get("summary") or "").strip()
        metrics = entry.get("metrics") or {}
        stars = metrics.get("stars", 0)
        forks = metrics.get("forks", 0)
        openi = metrics.get("open-issues", 0)
        updated = metrics.get("updated", "")
        tier = entry["tier"]
        latest = _newest_release(entry)
        vlo, vhi = _range_indices(entry)
        health = _health(entry, today)
        cost = _cost_text(entry)

        vpill = ""
        if tier >= 2 and latest:
            vpill = (f'<span class="vpill"><span class="c" aria-hidden="true">✓</span>'
                     f'verified {_rel_time(latest["published"], today)}</span>')
        meta_bits = []
        if health:
            color, label = health
            meta_bits.append(f'<span style="color:{color}">'
                             f'<span class="hdot" aria-hidden="true" style="background:{color}"></span>'
                             f'{label}</span>')
        if updated:
            meta_bits.append(f'updated {_rel_time(updated, today)}')
        if metrics:
            meta_bits.append(
                f'<span aria-label="{stars} GitHub stars, {forks} forks, '
                f'{openi} open issues and pull requests">'
                f'<span aria-hidden="true">★</span> {stars} · {forks} forks '
                f'· {openi} open issues &amp; PRs</span>')

        # Search blob: component, display name (when a manifest provides
        # one), summary, and maintainer names/handles.
        display = listing.get("name") or ""
        handles = " ".join(
            str(m.get(k, ""))
            for m in entry.get("maintainers", [])
            for k in ("name", "github", "gitlab") if m.get(k))
        text_blob = " ".join(f"{component} {display} {summary} {handles}".lower().split())
        rows_html.append(f"""
<a class="row-item" href="/plugin/{component}.html"
   data-name="{escape(component.lower())}" data-text="{escape(text_blob)}"
   data-group="{escape(plugintype)}" data-tier="{tier}"
   data-stars="{stars}" data-updated="{escape(updated)}"
   data-vlo="{vlo}" data-vhi="{vhi}"
   data-labels="{escape(' '.join(entry.get('labels', [])))}">
  <div class="row-main">
    <div class="row-line1"><span class="row-name">{escape(component)}</span>
      {_tier_badge(tier)}{vpill}</div>
    {f'<div class="row-summary">{escape(summary)}</div>' if summary else ''}
    <div class="row-meta">{' '.join(f'<span>{b}</span>' for b in meta_bits)}</div>
  </div>
  {f'<div class="row-rail"><span class="row-cost">{escape(cost)}</span></div>'
   if cost else ''}
</a>""")

    browse_js = BROWSE_JS % {"vorder": json.dumps(VORDER)}

    body = f"""
{_header()}
<div class="wrap">
<main id="main-content" tabindex="-1">
  <div class="hero">
    <h1>Every Moodle plugin, checked against its own source.</h1>
    <p>CAMP is an independent, community-governed archive of {total:,} Moodle
    plugins. Nothing earns a verified tier until it’s been checked against
    the maintainer’s own published source, and every check is public.</p>
  </div>
  <h2 class="visually-hidden">Why use CAMP</h2>
  <div class="trust-band">
    <div><h3 class="kicker">Verified against source</h3>
      <p>Packages are rebuilt from the maintainer’s tagged release and
      hash-compared. A match is the only way to earn the badge.</p></div>
    <div><h3 class="kicker">No accounts, no tracking</h3>
      <p>Browsing and installing need no registration. The archive keeps no
      per-site data and mirrors see only anonymous downloads.</p></div>
    <div><h3 class="kicker">Mirrorable by anyone</h3>
      <p>The whole archive is a static file tree plus a public git index.
      One rsync job makes a full mirror.</p></div>
  </div>

  <div class="searchbox">
    <label class="visually-hidden" for="q">Search plugins</label>
    <span class="glyph" aria-hidden="true">⌕</span>
    <input id="q" type="search" autocomplete="off"
      placeholder="Search {total:,} plugins by name, purpose, or maintainer…">
  </div>

  <button class="filters-toggle" id="filters-toggle" aria-expanded="false">Filters</button>

  <div class="body-grid">
    <aside class="sidebar" aria-labelledby="filter-heading">
      <a class="skip-link skip-inline" href="#results">Skip filters to results</a>
      <h2 class="facet-h" id="filter-heading">Filter plugins</h2>
      <fieldset class="facet-group"><legend class="facet-label">Type</legend>
        <div class="facet-list">{type_facets}{more_html}</div></fieldset>
      <fieldset class="facet-group"><legend class="facet-label">Moodle version</legend>
        <div class="facet-list">{ver_facets}{ver_more}</div></fieldset>
      <fieldset class="facet-group"><legend class="facet-label">Trust tier</legend>
        <div class="facet-list">{tier_facets}</div></fieldset>
      <fieldset class="facet-group"><legend class="facet-label">Project health</legend>
        <div class="facet-list">{health_facets}</div>
        <a class="facet-help" href="/how-it-works.html#health">What do these
        mean?</a></fieldset>
      <fieldset class="facet-group"><legend class="facet-label">Cost model</legend>
        <div class="facet-list">{cost_facets}</div></fieldset>
    </aside>
    <div id="results" tabindex="-1">
      <h2 class="visually-hidden">Plugins</h2>
      <div class="results-head">
        <p class="results-count" id="count" role="status" aria-live="polite"
          aria-atomic="true">{total:,} plugins found.</p>
        <div class="sorts" role="group" aria-labelledby="sort-lbl">
          <span class="lbl" id="sort-lbl">Sort</span>
          <button class="sortbtn" data-sort="relevance" aria-pressed="false">Relevance</button>
          <button class="sortbtn" data-sort="stars" aria-pressed="false">Stars</button>
          <button class="sortbtn" data-sort="recent" aria-pressed="false">Recent</button>
          <button class="sortbtn" data-sort="az" aria-pressed="false">A–Z</button>
        </div>
      </div>
      <div class="chips" id="chips" style="display:none"></div>
      <div class="rows" id="rows">{''.join(rows_html)}</div>
      <button class="sortbtn" id="show-more" style="display:none;margin:18px auto">Show more</button>
      <noscript><p style="margin-top:16px;font-size:0.8125rem;color:var(--muted)">
        Showing the {len(rows_html)} highest-tier plugins. JavaScript builds
        the searchable list — or browse the
        <a href="/all.html">complete plain index</a>.</p></noscript>
      <div class="empty" id="empty" style="display:none">
        No plugins match these filters.
        <button class="sortbtn outline" id="clear-empty">Clear all filters</button>
      </div>
    </div>
  </div>
</main>
{_footer(wrap=False)}
</div>
"""
    page = _page("CAMP — Community Archive of Moodle Plugins", body,
                 description="An independent, mirrorable archive of Moodle "
                 "plugins, source-verified byte for byte.",
                 extra_js=browse_js)
    return page, records


# ------------------------------------------------------------- detail ------


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


def _fg_for(color: str) -> str:
    """Black-or-white foreground for an externally supplied badge colour —
    whichever contrasts more (item 18); non-hex input falls back to white."""
    try:
        hx = color.lstrip("#")
        if len(hx) == 3:
            hx = "".join(c * 2 for c in hx)
        r, g, b = (int(hx[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except (ValueError, IndexError):
        return "#fff"
    def lin(u):
        return u / 12.92 if u <= 0.04045 else ((u + 0.055) / 1.055) ** 2.4
    lum = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
    white = 1.05 / (lum + 0.05)
    black = (lum + 0.05) / 0.05
    return "#fff" if white >= black else "#111"


def _review_badge(review: dict, component: str, badge_src) -> str:
    """MDL Shield's own badge (publish-fetched, sanitized, self-hosted) —
    or camp's HTML chip whenever the official artwork isn't available."""
    src = badge_src(review) if badge_src else None
    if src:
        return (f'<img class="msbadge" src="{escape(src)}" '
                f'alt="MDL Shield {escape(review["grade"])}">')
    return (f'<span class="abadge"><span class="l">MDL Shield</span>'
            f'<span class="m" style="background:{review["color"]};'
            f'color:{_fg_for(review["color"])}">'
            f'{escape(review["grade"])}</span></span>')


MDLSHIELD_REVIEWED_BADGE = "https://img.shields.io/badge/MDL%20Shield-Reviewed-0f766e"


def _reviewed_badge(badge_src) -> str:
    """The strip's generic review signal: grades are per-release claims,
    so the strip says only that published reviews exist. Same shields
    renderer and fetch/sanitize/self-host pipeline as the grade pills;
    camp's HTML chip when the artwork isn't available."""
    src = badge_src({"badge_url": MDLSHIELD_REVIEWED_BADGE}) if badge_src else None
    if src:
        return (f'<img class="msbadge" src="{escape(src)}" '
                f'alt="MDL Shield reviewed">')
    return (f'<span class="abadge"><span class="l">MDL Shield</span>'
            f'<span class="m" style="background:#0f766e;'
            f'color:{_fg_for("#0f766e")}">Reviewed</span></span>')


def _detail_page(entry: dict, listing: dict, base_url: str,
                 advisories: AdvisorySet, today: datetime.date,
                 checks_dir=None, shots=None, reviews=None,
                 badge_src=None, artifacts_base: str | None = None) -> str:
    component = entry["component"]
    artifacts_base = artifacts_base or f"{base_url}/artifacts"
    plugintype = component.partition("_")[0]
    name = listing.get("name") or component
    summary = (listing.get("summary") or entry.get("summary") or "").strip()
    latest = _newest_release(entry)
    package = _package_name(entry)
    tier = entry["tier"]
    metrics = entry.get("metrics") or {}
    health = _health(entry, today)
    upstream = metrics.get("latest-release") or {}
    check_doc = checks_mod.load(checks_dir, component)

    # Trust strip: the at-a-glance evaluation signals. The two trust
    # authorities render as two-segment shields matching the embeddable
    # badges (brand-coherent); tier 0 keeps the quiet plain pill — the
    # shield is the author's reward, same rule as the badge endpoint.
    if tier >= 1:
        # The literal embeddable badge (badge.py writes it every publish) —
        # the strip shows the real artifacts, camp's beside the reviewer's.
        meta_bits = [
            f'<a href="/how-it-works.html" class="msbadge-link">'
            f'<img class="msbadge" src="/badge/{escape(component)}.svg" '
            f'alt="camp {escape(badge_mod.TIER_BADGE_STYLE[tier][0])}"></a>']
    else:
        meta_bits = [_tier_badge(tier)]
    # The strip's review signal is deliberately generic: a grade is a
    # per-release claim, so the strip only says reviews exist and links
    # to MDL Shield's plugin page. The graded pills render where a
    # version is on screen — the install card and the Project history.
    if reviews:
        meta_bits.append(
            f'<a href="{MDLSHIELD_PLUGIN_URL}{escape(component)}" '
            f'class="msbadge-link">'
            f'{_reviewed_badge(badge_src)}</a>')
    # Health is a conclusion derived from update recency — it renders as
    # one phrase with its evidence, on its own line. Stars are popularity,
    # not trust: they live with the other repo metrics in Development.
    health_line = ""
    if health:
        color, label = health
        when = (f' · updated {_rel_time(metrics["updated"], today)}'
                if metrics.get("updated") else "")
        health_line = (f'<div class="health-line"><span style="color:{color}">'
                       f'<span class="hdot" aria-hidden="true" style="background:{color}"></span>'
                       f'{label}</span>{when}</div>')
    # Every declared disclosure label shows — not just the cost model —
    # on their own row beneath the strip.
    label_pills = "".join(
        f'<span class="lbl-pill">{escape(LABEL_NAMES[lab])}</span>'
        for lab in entry.get("labels") or [] if lab in LABEL_NAMES)
    labels_row = f'<div class="labels">{label_pills}</div>' if label_pills else ""
    license_id = entry.get("license", "")
    if license_id and not license_id.startswith(("GPL-", "AGPL-", "LGPL-")):
        meta_bits.append(
            f'<span class="tb tb-1">{escape(license_id)} · GPL-compatible</span>')

    # Banners: interruptions worth interrupting for.
    banners = ""
    if entry.get("status") == "moved":
        moved_to = entry["moved-to"]
        moved_link = (f'<a href="{escape(moved_to)}">{escape(moved_to)}</a>'
                      if moved_to.startswith("https://")
                      else f'<b>{escape(moved_to)}</b>')
        banners += f"""
  <div class="banner"><b>This plugin has moved.</b> New versions are published
  at {moved_link}. Versions already published here remain in the archive, stay
  installable, and continue to receive security advisories.</div>"""
    if tier == 0:
        claim_url = f"{AUTHORS_GUIDE_URL}#step-1--claim-the-listing-tier-0--tier-1"
        edit_url = f"{INDEX_REPO_URL}/edit/main/plugins/{plugintype}/{component}.yml"
        removal_url = (f"{INDEX_REPO_URL}/issues/new?template=removal-request.yml"
                       f"&title=Removal%20request%3A%20{component}")
        banners += f"""
  <div class="banner"><b>Discovered listing.</b> Found by scanning public
  sources; nothing is hosted here — installation happens from the
  author’s own repository. Are you the maintainer?
  <a href="{escape(claim_url)}">Claim this plugin</a> to publish verified
  releases (<a href="{escape(edit_url)}">edit your entry directly</a>), or
  <a href="{escape(removal_url)}">request removal</a> — no questions asked.</div>"""
    if tier >= 2 and latest and upstream.get("tag"):
        from .advisory import _version_key
        if _version_key(upstream["tag"]) > _version_key(latest["tag"]):
            banners += (f'<div class="banner">Upstream has published '
                        f'<b class="mono">{escape(upstream["tag"])}</b>, which the '
                        f'registry has not yet verified. The verified download here '
                        f'remains <b class="mono">{escape(latest["tag"])}</b>.</div>')

    # ---- install card + versions table (verified plugins) -----------------
    install = ""
    versions_table = ""
    if tier >= 2 and latest:
        cmd = f"composer require {package}"
        releases_data = []
        covered = set()
        for r in entry["releases"]:
            version = r["version"].split(" ")[0]
            if advisories.is_revoked(component, version):
                continue
            known = [v for v in r["supported-moodle"] if v in VORDER]
            if not known:
                continue
            covered.update(known)
            row = {
                "v": version.lstrip("v"), "tag": r["tag"],
                "commit": r["commit"][:12], "sha": r["zip-sha256"],
                "date": _fmt_date(r["published"]),
                "lo": VORDER.index(known[0]), "hi": VORDER.index(known[-1]),
                "zip": _zip_url(artifacts_base, component, version),
            }
            vcheck = checks_mod.for_version(check_doc, version.lstrip("v"))
            if vcheck:
                text, status = checks_mod.chip(vcheck)
                row["check"] = {"text": text, "status": status,
                                "tag": vcheck["tag"],
                                "phplint": vcheck.get("phplint", True),
                                "files": vcheck.get("files", 0),
                                "rules": vcheck.get("rules", {})}
            # The security review follows the selected release inside the
            # install card (prerendered so the server and the picker render
            # identically); absence renders nothing, per the feed's model.
            review = (reviews or {}).get(str(r.get("moodle-version", "")))
            if review:
                rev_href = escape(review["review_url"]
                                  or MDLSHIELD_PLUGIN_URL + component)
                row["review_html"] = (
                    f'<a href="{rev_href}" class="msbadge-link">'
                    f'{_review_badge(review, component, badge_src)}</a>'
                    f' <span class="rev-when">reviewed '
                    f'{escape(review["reviewed_at"])}</span>')
            releases_data.append(row)
        latest_v = latest["version"].split(" ")[0]
        if len(covered) > 1 or len(releases_data) > 1:
            options = "".join(
                f'<option value="{v}">{v}</option>'
                for v in VORDER[::-1] if v in covered)
            for_moodle = (f'<label class="inst-for" for="vpick">for Moodle</label> '
                          f'<select id="vpick">'
                          f'{options}</select>')
        else:
            for_moodle = (f'<span class="inst-for">for Moodle '
                          f'{escape(_moodle_range(latest))}</span>')
        newest_check = checks_mod.for_version(check_doc, latest_v.lstrip("v"))
        check_label = ('<span title="Static checks the registry runs at every '
                       'publish: PHP lint plus the Moodle Code Checker. '
                       '‘clean’ means no findings; style findings are '
                       'quality signals, not trust signals.">Code check</span> '
                       '<a href="/how-it-works.html" class="qmark" '
                       'aria-label="What is the code check?">?</a>: ')
        if newest_check:
            text, status = checks_mod.chip(newest_check)
            check_line = (f'<div class="inst-meta">{check_label}'
                          f'<b id="cc-text" class="chk-{status}">{escape(text)}</b>'
                          f' <span id="cc-meta"></span>'
                          f'<div class="cc-chips" id="cc-chips">'
                          f'{_check_chips(newest_check)}</div></div>')
        else:
            check_line = (f'<div class="inst-meta">{check_label}'
                          '<b id="cc-text" class="chk-muted">not yet '
                          'checked</b> <span id="cc-meta"></span>'
                          '<div class="cc-chips" id="cc-chips"></div></div>')
        latest_row = next((row for row in releases_data
                           if row["v"] == latest_v.lstrip("v")), None)
        latest_review = (latest_row or {}).get("review_html", "")
        # Absence means nothing, per the feed's privacy model: the line
        # exists only when at least one release has a published review.
        if any(row.get("review_html") for row in releases_data):
            review_line = (f'<div class="inst-meta" id="rev-line"'
                           f'{"" if latest_review else " hidden"}>'
                           f'<span id="rev-body">{latest_review}</span></div>')
        else:
            review_line = ""
        rel_json = json.dumps({"releases": releases_data, "vorder": VORDER,
                               "package": package})
        install = f"""
  <h2 class="visually-hidden">Download and compatibility</h2>
  <div class="install-card">
    <div class="left">
      <div class="inst-head"><span class="inst-ver" id="zip-ver">{escape(latest_v.lstrip("v"))}</span>
        {for_moodle}
        <span class="inst-for">Moodle <span id="compat">{escape(_moodle_range(latest))}</span></span></div>
      <div class="vline"><span class="c" aria-hidden="true">✓</span> Verified against source</div>
      <details class="vdetail"><summary>verification ledger</summary>
      <div class="ledger">
        <div class="lstep"><h3>Source tagged by maintainer</h3>
          <p>{escape(entry["source"].removeprefix("https://"))} @
          <span id="vd-tag">{escape(latest["tag"])}</span> · commit
          <span id="vd-commit">{latest["commit"][:12]}</span></p></div>
        <div class="lstep"><h3>Rebuilt deterministically from that tag</h3>
          <p>canonical ZIP, byte-identical on every rebuild</p></div>
        <div class="lstep"><h3>Artifact hash recorded in the public index</h3>
          <p>sha256 <span id="vd-sha">{latest["zip-sha256"]}</span></p></div>
        <div class="lstep planned"><h3>Release signed · trusted publishing</h3>
          <p>planned — TUF signing (RFC §4.3)</p></div>
        <div class="lstep planned"><h3>Recorded in public transparency log</h3>
          <p>planned — Sigstore/Rekor (RFC §4.3)</p></div>
      </div>
      <div class="lnote">Every step is independently verifiable. CAMP never
      modifies plugin code; it proves the ZIP you install is exactly what the
      maintainer published. Verified
      <span id="vd-date">{_fmt_date(latest["published"])}</span>.</div></details>
      {check_line}
      {review_line}
      <div class="pick-note" id="pick-note" style="display:none"></div>
      <div class="cmdline"><code id="cmd-text" tabindex="0" role="region"
        aria-label="Install command">{escape(cmd)}</code>
        <button id="copy-install" data-cmd="{escape(cmd)}">Copy</button></div>
      <div id="copy-status" class="visually-hidden" role="status"></div>
      <div id="ver-status" class="visually-hidden" role="status"></div>
    </div>
    <div class="right">
      <a class="btn act-secondary" id="zip-btn" href="{escape(_zip_url(artifacts_base, component, latest_v))}">Download ZIP</a>
    </div>
  </div>
  <script id="rel-data" type="application/json">{rel_json}</script>"""

        def _vrow(r):
            version = r["version"].split(" ")[0]
            v = version.lstrip("v")
            when = _fmt_date(r.get("released", r["published"]))
            rng = _moodle_range(r)
            if advisories.is_revoked(component, version):
                return (f'<li class="vrow revoked"><span class="v">{escape(v)}</span>'
                        f'<span class="d">{when}</span>'
                        f'<span class="d rng">Moodle {escape(rng)}</span>'
                        f'<span class="chk chk-bad">revoked</span>'
                        f'<span class="zl"></span>'
                        f'<span class="vm">Moodle {escape(rng)} · revoked</span></li>')
            vcheck = checks_mod.for_version(check_doc, v)
            if vcheck:
                text, status = checks_mod.chip(vcheck)
                chk = f'<span class="chk chk-{status}">{escape(text)}</span>'
                chk_vm = f" · {text}"
            else:
                chk = ('<span class="chk chk-muted" '
                       'title="not yet checked — the first code check runs at '
                       'the next publish">—</span>')
                chk_vm = ""
            # The whole row (minus the download link) is one native button:
            # keyboard-operable release selection with programmatic state.
            # The MDL Shield grade lives in the install card, following the
            # selection — a link cannot nest inside this button.
            return (f'<li class="vrow rel-row">'
                    f'<button type="button" class="vsel" data-ver="{escape(v)}" '
                    f'aria-pressed="false">'
                    f'<span class="v">{escape(v)}</span>'
                    f'<span class="d">{when}</span>'
                    f'<span class="d rng">Moodle {escape(rng)}</span>'
                    f'{chk}'
                    f'<span class="vm">Moodle {escape(rng)}{escape(chk_vm)}</span>'
                    f'</button>'
                    f'<a class="zl" href="{escape(_zip_url(artifacts_base, component, version))}" '
                    f'aria-label="Download version {escape(v)} as ZIP">ZIP</a></li>')
        from .advisory import _version_key
        ordered = sorted(entry["releases"], reverse=True,
                         key=lambda r: _version_key(r["version"].split(" ")[0]))
        if len(entry["releases"]) > 1:
            # The install picker only ever answers with the newest release
            # supporting a branch; the table leads with exactly that set so
            # its prominent rows are the ones the picker can name. Everything
            # else — superseded and revoked alike — stays archived one
            # disclosure away rather than competing for the download.
            best_by_branch = {}
            for row in releases_data:
                for i in range(row["lo"], row["hi"] + 1):
                    held = best_by_branch.get(i)
                    if held is None or _version_key(row["v"]) > _version_key(held["v"]):
                        best_by_branch[i] = row
            current_vs = {row["v"] for row in best_by_branch.values()}
            cur_rows, old_rows, revoked_n = [], [], 0
            for r in ordered:
                if r["version"].split(" ")[0].lstrip("v") in current_vs:
                    cur_rows.append(_vrow(r))
                else:
                    old_rows.append(_vrow(r))
                    if advisories.is_revoked(component, r["version"].split(" ")[0]):
                        revoked_n += 1
            older_html = ""
            if old_rows:
                n = len(old_rows)
                label = f'{n} older version{"s" if n != 1 else ""}'
                if revoked_n:
                    label += f' ({revoked_n} revoked)'
                label += ' · superseded by the releases above'
                older_html = (f'<details class="vmore"><summary>{label}'
                              f'</summary><ul class="vrows">'
                              f'{"".join(old_rows)}</ul></details>')
            vhead = ('<div class="vrow vhead"><span>Version</span>'
                     '<span>Released</span><span class="rng">Moodle</span>'
                     '<span class="chk">Code check</span><span></span></div>')
            versions_table = ('<h2 class="sect">All versions</h2>'
                              '<div class="vtable">' + vhead
                              + '<ul class="vrows">' + "".join(cur_rows)
                              + '</ul>' + older_html + '</div>')
    else:
        install = """
  <div class="install-card">
    <div class="left">
      <div class="vline warn" style="margin-top:0"><span class="c" aria-hidden="true">○</span>
        Not yet verified</div>
      <div class="vdetail">No package has been byte-matched to a public source
      release yet. Install from the author’s repository with additional
      caution.</div>
    </div>
  </div>"""

    # ---- story ------------------------------------------------------------
    gallery = ""
    if shots:
        # Uniform tiles in wrapping rows: compact at any count (schema caps
        # at 10), nothing hidden, nothing shrunk — the lightbox is the real
        # viewer. A lone screenshot gets a wider tile so it doesn't look
        # like a leftover.
        tiles = "".join(
            f'<a href="{escape(t["src"])}" data-lb="{i}" '
            f'data-caption="{escape(t.get("caption", ""))}"><img loading="lazy" '
            f'src="{escape(t["src"])}" alt="{escape(t.get("caption", "screenshot"))}"></a>'
            for i, t in enumerate(shots))
        single = " shots-single" if len(shots) == 1 else ""
        gallery = f'<div class="shots"><div class="shot-grid{single}">{tiles}</div></div>'

    # The summary shows once, under the title. About renders only when the
    # maintainer published a real description; otherwise a one-line
    # attribution note under the summary says where the text came from.
    description = listing.get("description") or ""
    source_link = f'<a href="{escape(entry["source"])}">source repository</a>'
    about = ""
    attrib = ""
    if description:
        about = (f'<h2 class="sect">About</h2>'
                 f'<div class="prose">{_render_description(description)}</div>')
    elif summary:
        attrib = (f'<div class="attrib">Summary from the source repository’s '
                  f'description — the maintainer has not published a listing '
                  f'manifest yet. See the {source_link} for full '
                  f'documentation.</div>')
    else:
        attrib = (f'<div class="attrib">No listing manifest published yet. '
                  f'See the {source_link} for documentation.</div>')

    advisory_html = ""
    advisory_items = advisories.for_component(component)
    if advisory_items:
        advisory_html = (f'<h2 class="sect">Security advisories</h2>'
                         f'{_advisory_cards(component, advisories)}')

    # ---- project facts: one full-width row per field -----------------------
    dev_bits = []
    if metrics:
        dev_bits.append(f'<span aria-label="{metrics.get("stars", 0)} GitHub stars">'
                        f'<span aria-hidden="true">★</span> '
                        f'{metrics.get("stars", 0)}</span> · '
                        f'{metrics.get("forks", 0)} forks · '
                        f'{metrics.get("open-issues", 0)} open issues & PRs')
    if upstream.get("tag"):
        when = f' · {_fmt_date(upstream["date"])}' if upstream.get("date") else ""
        dev_bits.append(f'Upstream release {escape(upstream["tag"])}{when}')

    badge_chips = []
    for b in (listing.get("badges") or []):
        if not isinstance(b, dict):
            continue
        if "mdlshield.com/" in b.get("endpoint", ""):
            # MDL Shield is registry-level now (the published-reviews feed);
            # an author-declared endpoint would only duplicate it — or add a
            # meaningless grey "not reviewed" chip, which the feed's privacy
            # model deliberately never renders.
            continue
        doc = badge_mod.fetch_endpoint(b.get("endpoint", ""))
        if doc is None:
            continue  # unfetchable or off-allowlist: omitted, never guessed
        chip = (f'<span class="abadge"><span class="l">{escape(doc["label"])}</span>'
                f'<span class="m" style="background:{doc["color"]};'
                f'color:{_fg_for(doc["color"])}">'
                f'{escape(doc["message"])}</span></span>')
        link = b.get("link", "")
        if link.startswith("https://"):
            chip = f'<a href="{escape(link)}">{chip}</a>'
        badge_chips.append(chip)

    kv_rows = []
    # Maintainer-declared links (listing schema): issues overrides the
    # {source}/issues guess; the rest render as a Links row below.
    declared_links = listing.get("links") or {}
    issues_url = declared_links.get("issues") or entry["source"] + "/issues"
    maintainers = entry.get("maintainers") or []
    if maintainers:
        m = maintainers[0]
        mt_name = m.get("name") or m.get("github") or m.get("gitlab") or "maintainer"
        others = len(maintainers) - 1
        rel_n = len(entry["releases"])
        sub_bits = []
        if rel_n:
            sub_bits.append(f'{rel_n} release{"s" if rel_n != 1 else ""} in the archive')
        if others > 0:
            sub_bits.append(f'+{others} co-maintainer{"s" if others != 1 else ""}')
        kv_rows.append(
            f'<div class="kvrow"><span class="fk">Maintainer</span>'
            f'<span class="fv"><div class="mt-name">{escape(mt_name)}</div>'
            f'{f"<div class=\"mt-sub\">{escape(chr(183).join(sub_bits)) if False else escape(" · ".join(sub_bits))}</div>" if sub_bits else ""}'
            f'</span></div>')
    kv_rows.append(f'<div class="kvrow"><span class="fk">Source repository</span>'
                   f'<span class="fv mono" style="font-size:0.78125rem;word-break:break-all">'
                   f'<a href="{escape(entry["source"])}">'
                   f'{escape(entry["source"].removeprefix("https://"))}</a></span></div>')
    link_bits = []
    for key, label in (("docs", "Documentation"), ("changelog", "Changelog"),
                       ("donate", "Support the author")):
        url = declared_links.get(key, "")
        if url.startswith("https://"):
            link_bits.append(f'<a href="{escape(url)}">{escape(label)}</a>')
    if link_bits:
        kv_rows.append('<div class="kvrow"><span class="fk">Links</span>'
                       f'<span class="fv">{" · ".join(link_bits)}'
                       '<div class="attrib" style="margin-top:6px">declared by the '
                       'maintainer</div></span></div>')
    kv_rows.append('<div class="kvrow"><span class="fk">Issues</span>'
                   f'<span class="fv"><a href="{escape(issues_url)}">'
                   'Browse known issues or report a problem</a></span></div>')
    if dev_bits:
        kv_rows.append('<div class="kvrow"><span class="fk">Development</span>'
                       f'<span class="fv">{" · ".join(dev_bits)}</span></div>')
    if not advisory_items:
        kv_rows.append('<div class="kvrow"><span class="fk">Security advisories</span>'
                       '<span class="fv">None published</span></div>')
    if reviews and latest:
        # The Project row is the complete public record: every published
        # review, newest first, each linking its own review — while the strip
        # shows only the current release's and the install card follows the
        # selection. The reviewer's subject is always the moodle.org
        # distribution, never camp's tag-built ZIP.
        ledger_mvs = {str(r.get("moodle-version", "")) for r in entry["releases"]}
        rev_items = []
        for mv in sorted(reviews, key=lambda m: reviews[m]["reviewed_at"],
                         reverse=True):
            review = reviews[mv]
            chip_target = review["review_url"] or MDLSHIELD_PLUGIN_URL + component
            chip = (f'<a href="{escape(chip_target)}" class="msbadge-link">'
                    f'{_review_badge(review, component, badge_src)}</a>')
            when = (f' · {escape(review["reviewed_at"])}'
                    if review["reviewed_at"] else "")
            note = "" if mv in ledger_mvs else " · not in the archive"
            rev_items.append(
                f'<div class="rev-item"><span class="abadges">{chip}</span> '
                f'{escape(review["release"])}{when}{note}</div>')
        plural = "s" if len(rev_items) != 1 else ""
        kv_rows.append(
            '<div class="kvrow"><span class="fk">Security review</span>'
            f'<span class="fv">{"".join(rev_items)}'
            f'<div class="attrib" style="margin-top:6px">published review{plural} '
            f'of the moodle.org distribution · fetched by the registry from '
            f'mdlshield.com · '
            f'<a href="{MDLSHIELD_PLUGIN_URL}{escape(component)}">more at '
            f'mdlshield.com</a></div></span></div>')
    if badge_chips:
        kv_rows.append('<div class="kvrow"><span class="fk">Author badges</span>'
                       f'<span class="fv"><span class="abadges">{"".join(badge_chips)}'
                       f'</span><div class="attrib" style="margin-top:6px">declared by '
                       f'the maintainer · fetched {escape(today.isoformat())}</div>'
                       f'</span></div>')
    project = ('<h2 class="sect">Project</h2><div class="kv">'
               + "".join(kv_rows) + '</div>')

    body = f"""
{_header()}
<div class="detail">
<main id="main-content" tabindex="-1">
  <a class="backlink" href="/">← Back to archive</a>
  <div class="crumb">{escape(_type_label(plugintype))}</div>
  <h1>{escape(name)}</h1>
  {f'<div class="mono" style="color:var(--faint-label);font-size:0.8125rem;margin-top:4px">{escape(component)}</div>' if name != component else ''}
  <div class="strip">{''.join(meta_bits)}</div>
  {health_line}
  {labels_row}
  {f'<p class="dsummary">{escape(summary)}</p>' if summary else ''}
  {attrib}
  {banners}
  {install}
  {versions_table}
  {gallery}
  {about}
  {advisory_html}
  {project}
</main>
{_footer(wrap=False)}
</div>
"""
    return _page(f"{name} — CAMP", body,
                 description=summary[:200] if summary else "",
                 extra_js=COPY_JS + LIGHTBOX_JS)


# ---------------------------------------------------------------- how ------


def _how_page() -> str:
    body = f"""
{_header()}
<div class="narrow how">
<main id="main-content" tabindex="-1">
  <a class="backlink" href="/">← Back to archive</a>
  <h1>How CAMP keeps the archive trustworthy</h1>
  <p class="lead">CAMP exists to answer one question with confidence: is the
  plugin you are about to install the same code its maintainer actually
  published? Here is how that guarantee is built.</p>

  <h2 class="visually-hidden">Why CAMP is trustworthy</h2>
  <div class="cards3">
    <div class="tcard"><h3 class="kicker"><span aria-hidden="true">✓ </span>Verified against source</h3>
      <p>Every published package is rebuilt deterministically from the
      maintainer’s tagged source and byte-compared. The hash match is
      public and anyone can reproduce it.</p></div>
    <div class="tcard"><h3 class="kicker">No accounts, no tracking</h3>
      <p>No registration to browse or install. Security warnings work by
      downloading the full advisory feed and matching locally — the
      archive never learns what your site runs.</p></div>
    <div class="tcard"><h3 class="kicker">Mirrorable by anyone</h3>
      <p>The archive is a static file tree plus a public git index. A full
      mirror is one rsync job, and mirrors need no trust: clients verify
      content, not servers.</p></div>
  </div>

  <h2>The verification pipeline</h2>
  <div class="step"><span class="num">1</span><div>
    <h3>Discover</h3><p>We index plugins from the public Moodle ecosystem and
    record where each one’s source lives.</p></div></div>
  <div class="step"><span class="num">2</span><div>
    <h3>Fetch the source</h3><p>For each release, we retrieve the exact
    package and the corresponding tag and commit from the maintainer’s
    repository.</p></div></div>
  <div class="step"><span class="num">3</span><div>
    <h3>Compare byte for byte</h3><p>The archived package is rebuilt and
    hash-compared against the public source. A match is what earns a plugin
    its verified trust tier.</p></div></div>
  <div class="step"><span class="num">4</span><div>
    <h3>Record and re-check</h3><p>Results are stored in an append-only
    ledger with timestamps and re-verified over time, so trust reflects the
    current state — not a one-off check.</p></div></div>

  <div class="bigcard">
    <h2 style="margin-top:0">The trust tiers</h2>
    <p style="color:var(--muted);font-size:0.90625rem">Each tier answers one
    question: does it exist, is someone accountable for it, does the artifact
    provably match its public source, have humans read the code.</p>
    <div class="tiergrid">
      <div class="tmini">{_tier_badge(0)}<p>Found by the discovery scanner in
        the public ecosystem. Metadata only — no maintainer has claimed
        it yet, and nothing is hosted.</p></div>
      <div class="tmini">{_tier_artwork(1)}<p>A maintainer has claimed
        ownership, declared a security contact and disclosure labels, and
        linked the canonical source repository.</p></div>
      <div class="tmini">{_tier_artwork(2)}<p>The archived package was
        automatically confirmed to match the public source, byte for
        byte.</p></div>
      <div class="tmini">{_tier_artwork(3)}<p>Verified and additionally
        reviewed by two independent members of the community review
        board.</p></div>
    </div>
  </div>


  <div class="bigcard">
    <h2 style="margin-top:0" id="health">Project health</h2>
    <p style="color:var(--muted);font-size:0.90625rem">One phrase computed
    from how recently the source repository changed, refreshed by the
    registry's daily metrics sync. It measures momentum, not quality — a
    stable plugin can be quiet and excellent.</p>
    <div class="tiergrid">
      <div class="tmini"><span class="hterm" style="color:var(--ok-text)"><span class="hdot" aria-hidden="true" style="background:var(--ok-text)"></span>Actively maintained</span><p>The source repository saw activity within the last 6 months.</p></div>
      <div class="tmini"><span class="hterm" style="color:var(--ok-text)"><span class="hdot" aria-hidden="true" style="background:var(--ok-text)"></span>Maintained</span><p>Activity within the last 18 months.</p></div>
      <div class="tmini"><span class="hterm" style="color:var(--warn-text)"><span class="hdot" aria-hidden="true" style="background:var(--warn-text)"></span>Slowing down</span><p>No activity for 18 months to 3 years.</p></div>
      <div class="tmini"><span class="hterm" style="color:var(--bad-text)"><span class="hdot" aria-hidden="true" style="background:var(--bad-text)"></span>Dormant</span><p>No activity for more than 3 years.</p></div>
      <div class="tmini"><span class="hterm" style="color:var(--text-subtle)"><span class="hdot" aria-hidden="true" style="background:var(--text-subtle)"></span>Archived upstream</span><p>The maintainer archived the repository — it is read-only and no longer developed, whatever its age.</p></div>
    </div>
    <p style="color:var(--muted);font-size:0.8125rem;margin-bottom:0">Plugins
    the registry has no activity data for show no phrase at all — unknown is
    not the same as dormant.</p>
  </div>

  <div class="cta">
    <h2 style="margin:0">Ready to find a plugin?</h2>
    <p style="color:var(--muted);margin-top:8px">Search the archive, filter by
    trust tier, and see every check before you install.</p>
    <a href="/">Browse the archive</a>
  </div>
</main>
{_footer(wrap=False)}
</div>
"""
    return _page("How it works — CAMP", body,
                 description="How CAMP verifies every Moodle plugin against "
                 "its own public source.")


# ------------------------------------------------------------ generate -----


def generate(index_dir: str | Path, base_url: str, out_dir: str | Path,
             listings_dir: str | Path | None = None,
             checks_dir: str | Path | None = None,
             reviews_source: str | None = None,
             artifacts_base: str | None = None) -> int:
    out = Path(out_dir)
    (out / "plugin").mkdir(parents=True, exist_ok=True)
    listings = Path(listings_dir) if listings_dir else None
    advisories = AdvisorySet.load(index_dir)
    today = datetime.date.today()
    reviews_by_component = (reviews_mod.fetch_feed(reviews_source)
                           if reviews_source else None) or {}

    # Official MDL Shield badge artwork, self-hosted at /mdlshield/<hash>.svg.
    # Content-addressed by badge_url, and the previously published site is
    # the cache: shields.io is contacted only for badges never seen before.
    mdlshield_out = out / "mdlshield"
    _badge_cache: dict[str, str | None] = {}

    def badge_src(review: dict) -> str | None:
        url = review.get("badge_url") or ""
        if not url:
            return None
        if url in _badge_cache:
            return _badge_cache[url]
        import hashlib as _hashlib
        import urllib.request as _request
        rel = f"/mdlshield/{_hashlib.sha256(url.encode()).hexdigest()[:12]}.svg"
        raw = None
        try:                                   # previous publish first
            with _request.urlopen(_request.Request(
                    base_url + rel, headers={"User-Agent": "camp-badge-reuse"}),
                    timeout=10) as resp:
                raw = reviews_mod.sanitize_badge_svg(
                    resp.read(reviews_mod.MAX_BADGE_BYTES))
        except Exception:
            raw = None
        if raw is None:
            raw = reviews_mod.fetch_badge_svg(url)
        if raw is None:
            _badge_cache[url] = None
            return None
        mdlshield_out.mkdir(parents=True, exist_ok=True)
        (mdlshield_out / rel.rsplit("/", 1)[1]).write_bytes(raw)
        _badge_cache[url] = rel
        return rel

    entries: list[tuple[dict, dict]] = []
    for entry_path in sorted(Path(index_dir).glob("plugins/*/*.yml")):
        entry = load_entry(entry_path)
        # 'moved' listings keep their pages (with a successor notice);
        # only 'delisted' disappears from the generated site.
        if entry.get("status", "active") == "delisted":
            continue
        listing = _load_listing(listings, entry["component"])
        entries.append((entry, listing))

    entries.sort(key=lambda pair: (pair[1].get("name") or pair[0]["component"]).lower())

    shots_src = (listings / "screenshots") if listings else None
    if shots_src and shots_src.exists():
        shutil.copytree(shots_src, out / "shots", dirs_exist_ok=True)

    for entry, listing in entries:
        component = entry["component"]
        shots = []
        for shot in (listing.get("screenshots") or []):
            stem = Path(shot["path"]).stem
            if shots_src and (shots_src / component / f"{stem}.png").exists():
                shots.append({"src": f"/shots/{component}/{stem}.png",
                              "caption": shot.get("caption", "")})
        page = _detail_page(entry, listing, base_url, advisories, today,
                            checks_dir=checks_dir, shots=shots,
                            reviews=reviews_by_component.get(component),
                            badge_src=badge_src, artifacts_base=artifacts_base)
        (out / "plugin" / f"{component}.html").write_text(page)

    browse_html, records = _browse_page(entries, today)
    (out / "index.html").write_text(browse_html)
    (out / "index.json").write_text(json.dumps({"plugins": records},
                                               separators=(",", ":")))
    (out / "version.json").write_text(json.dumps({
        "camp-tools": TOOLS_VERSION,
        "built": _BUILT.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plugins": len(entries)}) + "\n")

    all_rows = "".join(
        f'<li><a class="mono" href="/plugin/{e["component"]}.html">'
        f'{escape(e["component"])}</a> — Tier {e["tier"]}</li>'
        for e, _ in sorted(entries, key=lambda p: p[0]["component"]))
    (out / "all.html").write_text(_page(
        "All plugins — CAMP",
        f'{_header()}<div class="narrow"><main id="main-content" tabindex="-1">'
        f'<h1 style="font-family:var(--serif);'
        f'color:var(--ink)">All {len(entries):,} plugins</h1>'
        f'<ul style="margin-top:18px;line-height:2;list-style:none">{all_rows}</ul>'
        f'</main>{_footer(wrap=False)}</div>'))

    (out / "how-it-works.html").write_text(_how_page())

    badge_mod.write_badges(index_dir, out / "badge")
    if checks_dir:
        for entry, _ in entries:
            doc = checks_mod.load(checks_dir, entry["component"])
            newest = _newest_release(entry)
            summary = checks_mod.for_version(
                doc, newest["version"].split(" ")[0].lstrip("v")) if newest else None
            if summary:
                text, status = checks_mod.chip(summary)
                doc = {"schemaVersion": 1, "label": "CAMP check",
                       "message": text,
                       "color": checks_mod.STATUS_COLORS[status]}
                (out / "badge").mkdir(parents=True, exist_ok=True)
                (out / "badge" / f'{entry["component"]}-checks.json').write_text(
                    json.dumps(doc, sort_keys=True) + "\n")
                (out / "badge" / f'{entry["component"]}-checks.svg').write_text(
                    badge_mod.render_svg(doc["label"], doc["message"], doc["color"]))

    fonts_src = Path(__file__).resolve().parent / "fonts"
    if fonts_src.exists():
        shutil.copytree(fonts_src, out / "fonts", dirs_exist_ok=True)

    icons_src = Path(__file__).resolve().parent / "icons"
    if icons_src.exists():
        for icon in icons_src.iterdir():
            shutil.copy(icon, out / icon.name)

    return len(entries)
