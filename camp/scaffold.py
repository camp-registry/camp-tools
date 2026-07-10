"""Author-side scaffolding: starter listing manifest and .gitattributes.

The RFC (§4.1, §12) promises that release tooling scaffolds a starter
`.camp/listing.yml` for repos that lack one, and that dev files can be kept
out of distribution ZIPs via ordinary `.gitattributes export-ignore` rules
(the canonical builder honours them).
"""

from __future__ import annotations

import re
from pathlib import Path

EXPORT_IGNORE = [
    "# Keep development files out of camp distribution ZIPs (git archive semantics)",
    ".github export-ignore",
    ".gitattributes export-ignore",
    ".gitignore export-ignore",
    ".camp export-ignore",
    "tests export-ignore",
    "node_modules export-ignore",
]

STARTER_LISTING = """\
# camp listing manifest — descriptive content for your plugin's page.
# Edit freely and commit; camp ingests this file at each tagged release.
# Schema: https://<camp-repo>/schema/listing.schema.json
name: {name}
summary: {summary}
description: |
  Describe what the plugin does, in markdown (no raw HTML).
labels:
  # Required disclosure labels — pick all that apply (RFC §4.7):
  #   fully-free | freemium | paid-service | external-account
  #   donation-supported | commercial-support-available
  - fully-free
# screenshots:
#   - path: .camp/screenshots/overview.png
#     caption: Main view
# links:
#   docs: https://…
#   issues: https://…
"""


def _guess_name(source: Path, component: str | None) -> tuple[str, str]:
    """(name, summary) guesses from version.php and the README title line."""
    name = component or source.resolve().name
    version_php = source / "version.php"
    if component is None and version_php.exists():
        match = re.search(r"\$plugin->component\s*=\s*['\"]([a-z0-9_]+)['\"]",
                          version_php.read_text(errors="replace"))
        if match:
            name = match.group(1)
    summary = "One line about what this plugin does."
    for readme in ("README.md", "README.txt", "README"):
        path = source / readme
        if path.exists():
            lines = [l.strip("# ").strip() for l in path.read_text(errors="replace").splitlines()
                     if l.strip() and not l.startswith(("![", "[!"))]
            if lines:
                if len(lines) > 1 and len(lines[1]) > 20:
                    summary = lines[1][:200]
                break
    return name, summary


def scaffold(source_dir: str | Path, component: str | None = None,
             check_only: bool = False) -> tuple[list[str], list[str]]:
    """Returns (created/needed, already-fine) descriptions."""
    source = Path(source_dir)
    actions: list[str] = []
    fine: list[str] = []

    listing = source / ".camp" / "listing.yml"
    if listing.exists():
        fine.append(f"{listing} exists")
    else:
        actions.append(f"{listing}: starter manifest" + ("" if check_only else " written"))
        if not check_only:
            name, summary = _guess_name(source, component)
            listing.parent.mkdir(exist_ok=True)
            listing.write_text(STARTER_LISTING.format(name=name, summary=summary))

    gitattributes = source / ".gitattributes"
    existing = gitattributes.read_text() if gitattributes.exists() else ""
    if "export-ignore" in existing:
        fine.append(f"{gitattributes} already has export-ignore rules")
    else:
        actions.append(f"{gitattributes}: export-ignore rules" + ("" if check_only else " appended"))
        if not check_only:
            with open(gitattributes, "a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write("\n".join(EXPORT_IGNORE) + "\n")

    return actions, fine
