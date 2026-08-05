"""Self-contained operator review page for the scan ledger
(camp-tools#31): `camp scan-report --html FILE`.

The terminal report answers "what happened"; this page is for working
the queues. Everything review-shaped gets a section with linked
repositories, evidence, and dates, with the needs-review class grouped
by the reason that parked each row, because the reason decides which
runbook applies. Bulk outcome classes render as counts only so the file
stays reviewable. Local file, nothing published, nothing fetched.
"""

from __future__ import annotations

import datetime
from html import escape
from pathlib import Path

from .scan import listed_unknown_types, load_ledger, unknown_type_families

# needs-review detail prefixes -> the queue the row belongs to, in
# review-priority order. The detail strings are the scanner's own; a new
# gate whose wording is not registered here lands in "other reasons",
# which is the prompt to add it.
REASONS = [
    ("directory-anchor", "Directory-anchor mismatches (repoints runbook)",
     "the old moodle.org directory published"),
    ("unknown-type", "Unknown plugin types (family establishment, camp-tools#16)",
     "unknown plugin type"),
    ("bundled-shadow", "Bundled-name shadowing (camp-tools#16)",
     "declares "),  # refined below: shadow details contain 'bundles a subplugin'
    ("core-since", "Core components, standard since mid-window (camp-tools#29)",
     "declares "),  # refined below: 'bundled with Moodle since'
    ("name-mismatch", "Repository name does not match the component (RFC §8)",
     "declares "),
]
BULK = {"no-version-php", "bad-license", "exists", "written", "skipped-known"}


def _reason(detail: str) -> str:
    if detail.startswith("the old moodle.org directory published"):
        return "directory-anchor"
    if detail.startswith("unknown plugin type"):
        return "unknown-type"
    if "bundles a subplugin of the same name" in detail:
        return "bundled-shadow"
    if "bundled with Moodle since" in detail:
        return "core-since"
    if "repo name does not correspond" in detail:
        return "name-mismatch"
    return "other"


def _repo_link(full_name: str, record: dict) -> str:
    host = record.get("host") or (
        "gitlab.com" if full_name.count("/") > 1 else "github.com")
    return (f'<a href="https://{host}/{escape(full_name)}">'
            f'{escape(full_name)}</a>')


def _table(rows: list[str]) -> str:
    return ('<div class="tblwrap"><table><thead><tr><th>repository</th>'
            '<th>component</th><th>evidence</th><th>seen</th></tr></thead>'
            '<tbody>' + "".join(rows) + '</tbody></table></div>')


def _row(full_name: str, record: dict) -> str:
    return (f'<tr><td>{_repo_link(full_name, record)}</td>'
            f'<td class="mono">{escape(record.get("component", ""))}</td>'
            f'<td>{escape(record.get("detail", ""))}</td>'
            f'<td class="dates">{escape(record.get("first-seen", ""))}'
            f' → {escape(record.get("last-checked", ""))}</td></tr>')


def _section(anchor: str, title: str, rows: list[str], note: str = "") -> str:
    if not rows:
        return ""
    return (f'<section id="{anchor}"><h2>{escape(title)} '
            f'<span class="n">({len(rows)})</span></h2>'
            + (f'<p class="note">{escape(note)}</p>' if note else "")
            + f'<input class="filter" type="search" '
            f'placeholder="filter {len(rows)} rows…" data-target="{anchor}">'
            + _table(rows) + '</section>')


def render(index_dir: str | Path) -> str:
    ledger = load_ledger(index_dir)
    by_outcome: dict[str, list] = {}
    for repo, record in sorted(ledger.items()):
        by_outcome.setdefault(record.get("outcome", "?"), []).append((repo, record))

    counts = "".join(
        f'<div class="tile"><div class="k">{len(v)}</div>'
        f'<div class="l">{escape(k)}</div></div>'
        for k, v in sorted(by_outcome.items(), key=lambda kv: -len(kv[1])))

    by_reason: dict[str, list[str]] = {}
    for repo, record in by_outcome.get("needs-review", []):
        by_reason.setdefault(_reason(record["detail"]), []).append(_row(repo, record))

    sections = []
    titles = dict((key, title) for key, title, _ in REASONS)
    titles["other"] = "Other needs-review reasons (unregistered wording — extend the report)"
    for key in [k for k, _, _ in REASONS] + ["other"]:
        sections.append(_section(f"nr-{key}", titles[key], by_reason.get(key, [])))

    families = unknown_type_families(index_dir)
    fam_rows = []
    for prefix, members in families.items():
        for repo, record in members:
            fam_rows.append(_row(repo, record))
    sections.append(_section(
        "families", "Family establishment queue", fam_rows,
        "One establishment review per prefix; record approved families in "
        "discovery/subplugin-families.yml."))

    legacy = listed_unknown_types(index_dir)
    legacy_rows = []
    for prefix, components in legacy.items():
        listing = ", ".join(components) or "(empty type directory, removable)"
        legacy_rows.append(
            f'<tr><td class="mono">{escape(prefix)}</td><td></td>'
            f'<td>{escape(listing)}</td><td></td></tr>')
    sections.append(_section(
        "legacy", "Already-listed unknown types (hygiene, camp-index#21)",
        legacy_rows, "Establish the family or route members through removal."))

    for outcome, title in (("core-component", "Core-component rejections (camp-tools#29)"),
                           ("copy", "Copies (collision ledger)"),
                           ("name-collision", "Name collisions (NAMESPACE.md)"),
                           ("opted-out", "Opted out (removals)")):
        rows = [_row(repo, record) for repo, record in by_outcome.get(outcome, [])]
        sections.append(_section(outcome, title, rows))

    bulk_note = ", ".join(f"{k} {len(by_outcome.get(k, []))}"
                          for k in sorted(BULK) if by_outcome.get(k))
    generated = datetime.date.today().isoformat()
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>camp scan review · {generated}</title>
<style>
:root{{--ink:#1a1a1a;--muted:#666;--border:#ddd;--bg:#fff;--surface:#f6f6f4;--accent:#0f766e}}
@media (prefers-color-scheme: dark){{:root{{--ink:#e8e8e6;--muted:#999;--border:#333;--bg:#151514;--surface:#1e1e1c;--accent:#2dd4bf}}}}
body{{font:15px/1.5 system-ui,sans-serif;color:var(--ink);background:var(--bg);margin:0;padding:28px;max-width:1200px;margin-inline:auto}}
h1{{font-size:1.3rem}} h2{{font-size:1.05rem;margin:34px 0 4px}}
.n{{color:var(--muted);font-weight:400}}
.tiles{{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}}
.tile{{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:8px 14px;min-width:90px}}
.tile .k{{font-size:1.15rem;font-weight:700}} .tile .l{{font-size:.75rem;color:var(--muted);font-family:ui-monospace,monospace}}
.note{{color:var(--muted);font-size:.85rem;margin:2px 0 6px}}
.filter{{margin:6px 0;padding:6px 9px;border:1px solid var(--border);border-radius:3px;background:var(--bg);color:var(--ink);width:280px;font:inherit;font-size:.85rem}}
.tblwrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:.85rem}}
th{{text-align:left;font-family:ui-monospace,monospace;font-size:.72rem;color:var(--muted);border-bottom:1px solid var(--border);padding:5px 10px 5px 0}}
td{{border-bottom:1px solid var(--border);padding:6px 10px 6px 0;vertical-align:top}}
td.mono{{font-family:ui-monospace,monospace;font-size:.8rem}} td.dates{{white-space:nowrap;color:var(--muted);font-size:.78rem}}
a{{color:var(--accent)}}
footer{{margin-top:36px;color:var(--muted);font-size:.78rem}}
</style></head><body>
<h1>camp scan review <span class="n">· {len(ledger)} repositories evaluated · generated {generated}</span></h1>
<div class="tiles">{counts}</div>
<p class="note">Bulk classes (counts above, no tables): {escape(bulk_note)}.</p>
{"".join(sections)}
<footer>Generated by camp scan-report --html (camp-tools#31). Local operator file; runbooks: camp-docs/runbooks/.</footer>
<script>
document.querySelectorAll('.filter').forEach(function(f){{
  f.addEventListener('input', function(){{
    var q = f.value.toLowerCase();
    document.querySelectorAll('#' + f.dataset.target + ' tbody tr').forEach(function(tr){{
      tr.hidden = q && tr.textContent.toLowerCase().indexOf(q) === -1;
    }});
  }});
}});
</script>
</body></html>
"""


def write(index_dir: str | Path, out_path: str | Path) -> None:
    Path(out_path).write_text(render(index_dir))
