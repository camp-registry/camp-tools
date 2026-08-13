"""Utilities on the generated site (camp-docs#4): detail pages, browse
integration via the sibling index.json key, disclosure rendering for
closed-source entries, and the no-utilities build staying identical."""

import json
import re
from html.parser import HTMLParser

import yaml

from camp.site import generate as site_generate


def _add_utilities(index_dir, entries):
    d = index_dir / "utilities"
    d.mkdir()
    for entry in entries:
        (d / f'{entry["name"]}.yml').write_text(yaml.safe_dump(entry))


MOOSH = {
    "name": "moosh",
    "display-name": "Moosh",
    "summary": "MOOdle SHell.",
    "category": "cli",
    "source": "https://github.com/tmuras/moosh",
    "source-repo-id": 6603614,
    "homepage": "https://moosh-online.com",
    "install": ["composer", "git"],
    "license": "GPL-3.0",
    "first-seen": "2026-08-12",
    "metrics": {"stars": 257, "forks": 189, "open-issues": 39,
                "updated": "2026-08-04",
                "latest-release": {"tag": "1.27", "date": "2025-02-08"},
                "checked": "2026-08-13"},
}

import datetime

_RECENT = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()

MDLCODE = {
    "name": "mdlcode",
    "display-name": "MDLCode",
    "summary": "IDE extension. Closed source, free tier.",
    "category": "ide",
    "source": "https://github.com/lmscloud-io/mdlcode-docs",
    "source-repo-id": 650341787,
    "install": ["vscode-marketplace"],
    "release-channel": "openvsx:LMSCloud/mdlcode",
    "license": "Proprietary",
    "closed-source": True,
    "labels": ["freemium"],
    "first-seen": "2026-08-13",
    # as enrich writes it: the channel release date doubles as updated
    "metrics": {"checked": "2026-08-13",
                "updated": _RECENT,
                "latest-release": {
                    "tag": "1.6.4", "date": _RECENT,
                    "url": "https://open-vsx.org/extension/LMSCloud/mdlcode"}},
}


class _Scan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elems = []

    def handle_starttag(self, tag, attrs):
        self.elems.append((tag, dict(attrs)))


def test_utility_pages_and_browse_integration(index_dir, tmp_path):
    _add_utilities(index_dir, [MOOSH, MDLCODE])
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)

    page = (out / "utility" / "moosh.html").read_text()
    assert "Curated listing" in page
    assert "Utility listing." in page               # banner
    assert "Are you the maintainer?" in page        # unclaimed invite
    assert ">GPL-3.0<" in page
    assert 'href="/?group=utility"' in page         # crumb to the facet
    assert "releases/latest" in page and ">1.27<" in page

    index_html = (out / "index.html").read_text()
    assert 'data-value="utility"' in index_html     # Ecosystem facet
    assert "Ecosystem" in index_html
    assert "utilities.html" not in index_html       # no section page, no nav link

    data = json.loads((out / "index.json").read_text())
    slugs = [u["c"] for u in data["utilities"]]
    assert slugs == ["mdlcode", "moosh"]
    moosh = data["utilities"][1]
    assert moosh["k"] == "u" and moosh["g"] == "utility"
    assert moosh["a"] == -1 and moosh["b"] == -1    # version filters exclude


def test_closed_source_disclosure(index_dir, tmp_path):
    _add_utilities(index_dir, [MDLCODE])
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    page = (out / "utility" / "mdlcode.html").read_text()
    assert "Closed source" in page and "Freemium" in page
    assert "Project repository" in page
    assert "Source repository" not in page
    assert "source code itself is not published" in page
    assert "Proprietary · closed source" in page
    assert "open-vsx.org/extension/LMSCloud/mdlcode" in page  # url override
    # no repo metrics rendered for closed-source entries, but health
    # and recency come from the channel release date, worded honestly
    assert "★" not in page
    assert "Actively maintained" in page
    assert "released 5 d ago" in page
    assert "updated 5 d ago" not in page
    record = json.loads((out / "index.json").read_text())["utilities"][0]
    assert record["h"] == 1 and record["s"] == 0
    assert record["u"] == _RECENT


def test_utility_page_landmarks(index_dir, tmp_path):
    _add_utilities(index_dir, [MOOSH])
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    scanner = _Scan()
    scanner.feed((out / "utility" / "moosh.html").read_text())
    elems = scanner.elems
    mains = [a for t, a in elems if t == "main"]
    assert len(mains) == 1 and mains[0].get("id") == "main-content"
    assert any(t == "a" and a.get("href") == "#main-content" for t, a in elems)
    assert any(t == "nav" and a.get("aria-label") for t, a in elems)
    ids = [a["id"] for _, a in elems if "id" in a]
    assert len(ids) == len(set(ids))


def test_no_utilities_build_is_unchanged(index_dir, tmp_path):
    base = tmp_path / "base"
    site_generate(index_dir, "https://repo.test", base)
    with_utils = tmp_path / "with"
    _add_utilities(index_dir, [MOOSH])
    site_generate(index_dir, "https://repo.test", with_utils)

    # plugin surfaces byte-identical; index.json plugins array untouched
    assert ((base / "plugin" / "mod_example.html").read_bytes()
            == (with_utils / "plugin" / "mod_example.html").read_bytes())
    base_data = json.loads((base / "index.json").read_text())
    with_data = json.loads((with_utils / "index.json").read_text())
    assert "utilities" not in base_data
    assert base_data["plugins"] == with_data["plugins"]
    assert not (base / "utility").exists()
    assert "data-value=\"utility\"" not in (base / "index.html").read_text()


def test_stylesheet_type_floor_covers_utility_pill(index_dir, tmp_path):
    # Same lint as test_a11y's floor check (decorative glyph rules
    # excepted), run over the new page type.
    decorative = (".vpill .c", ".vline .c", ".lstep::before")
    _add_utilities(index_dir, [MOOSH])
    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out)
    css = "\n".join(re.findall(r"<style>(.*?)</style>",
                               (out / "utility" / "moosh.html").read_text(),
                               re.S))
    for chunk in css.split("}"):
        selector, _, decls = chunk.rpartition("{")
        if any(d in selector for d in decorative):
            continue
        for value, unit in re.findall(r"font-size:\s*([\d.]+)(px|rem|em)",
                                      decls):
            floor = 12 if unit == "px" else 0.75
            assert float(value) >= floor, (selector.strip(), value + unit)
