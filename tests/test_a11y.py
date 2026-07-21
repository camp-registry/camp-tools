"""Prompt 12: accessibility regression checks over the generated site.

Automated subset of the third-party WCAG 2.2 AA review's regression
prompt: landmarks and skip links, control labels, ARIA state on the
right controls (and absence on non-toggles), disclosure wiring, status
regions, decorative-glyph hiding, unique ids, heading order, token and
inline-hex contrast in both themes, and a stylesheet type-floor lint.

Not automatable here and covered by the manual matrix instead:
axe-core in a real browser (last run 2026-07-20 — 0 serious/critical
across all four page types in both themes), keyboard-only, NVDA and
VoiceOver, 200/400 % zoom, 320 px viewport, Windows High Contrast,
reduced motion, text-spacing overrides, JS disabled, failed
/index.json, clipboard denial.
"""
import math
import re
from html.parser import HTMLParser

import pytest

from camp.site import generate as site_generate
from camp.site import _fg_for

PAGES = ["index.html", "plugin/mod_example.html", "how-it-works.html",
         "all.html"]


@pytest.fixture
def site(index_dir, tmp_path):
    out = tmp_path / "a11y-site"
    site_generate(index_dir, "https://repo.test", out)
    return {p: (out / p).read_text() for p in PAGES}


class _Scan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elems = []

    def handle_starttag(self, tag, attrs):
        self.elems.append((tag, dict(attrs)))


def scan(html):
    s = _Scan()
    s.feed(html)
    return s.elems


def styles(html):
    return "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))


# ------------------------------------------------- structure ------------

def test_one_main_with_skip_link(site):
    for page, html in site.items():
        elems = scan(html)
        mains = [a for t, a in elems if t == "main"]
        assert len(mains) == 1, page
        assert mains[0].get("id") == "main-content", page
        assert any(t == "a" and a.get("href") == "#main-content"
                   for t, a in elems), page


def test_landmarks(site):
    for page, html in site.items():
        elems = scan(html)
        tags = [t for t, _ in elems]
        assert "header" in tags and "footer" in tags, page
        assert any(t == "nav" and a.get("aria-label")
                   for t, a in elems), page


def test_ids_unique_and_heading_order(site):
    for page, html in site.items():
        elems = scan(html)
        ids = [a["id"] for _, a in elems if "id" in a]
        dupes = {i for i in ids if ids.count(i) > 1}
        assert not dupes, (page, dupes)
        levels = [int(t[1]) for t, _ in elems if re.fullmatch(r"h[1-6]", t)]
        assert levels and levels[0] == 1, page
        for prev, cur in zip(levels, levels[1:]):
            assert cur <= prev + 1, (page, levels)


# ------------------------------------------------- controls -------------

def test_search_and_version_select_labelled(site):
    elems = scan(site["index.html"])
    assert any(t == "label" and a.get("for") == "q" for t, a in elems)
    elems = scan(site["plugin/mod_example.html"])
    labels = [a for t, a in elems if t == "label" and a.get("for") == "vpick"]
    assert labels and "visually-hidden" not in labels[0].get("class", "")


def test_no_inline_click_handlers(site):
    for page, html in site.items():
        assert "onclick" not in html, page


def test_version_rows_are_state_bearing_buttons(index_dir, tmp_path):
    # the All-versions table only renders with more than one release
    import yaml
    entry_path = next((index_dir / "plugins").glob("*/*.yml"))
    entry = yaml.safe_load(entry_path.read_text())
    second = dict(entry["releases"][0])
    second["version"] = "1.1.0"
    second["moodle-version"] = 2026011600
    entry["releases"].append(second)
    entry_path.write_text(yaml.dump(entry))
    out = tmp_path / "multi"
    site_generate(index_dir, "https://repo.test", out)
    elems = scan((out / "plugin" / "mod_example.html").read_text())
    vsels = [(t, a) for t, a in elems if "vsel" in a.get("class", "")]
    assert len(vsels) >= 2
    for t, a in vsels:
        assert t == "button" and "aria-pressed" in a


def test_toggle_state_only_on_toggle_buttons(site):
    elems = scan(site["index.html"])
    for t, a in elems:
        cls = a.get("class", "")
        if "facet" in cls.split() or a.get("data-sort"):
            assert a.get("aria-pressed") is not None, a
        if a.get("id") in ("show-more", "clear-empty"):
            assert "aria-pressed" not in a, a["id"]
    elems = scan(site["plugin/mod_example.html"])
    copy_btn = [a for t, a in elems if a.get("id") == "copy-install"]
    assert copy_btn and "aria-pressed" not in copy_btn[0]


def test_disclosures_wired(site):
    elems = scan(site["index.html"])
    ids = {a["id"] for _, a in elems if "id" in a}
    more = [a for t, a in elems if "facet-more" in a.get("class", "")]
    assert more
    for a in more:
        assert "aria-expanded" in a
        assert a.get("aria-controls") in ids


def test_status_regions_exist(site):
    elems = scan(site["index.html"])
    count = [a for _, a in elems if a.get("id") == "count"]
    assert count and count[0].get("role") == "status"
    assert count[0].get("aria-live") == "polite"
    elems = scan(site["plugin/mod_example.html"])
    for rid in ("copy-status", "ver-status"):
        region = [a for _, a in elems if a.get("id") == rid]
        assert region and region[0].get("role") == "status", rid


def test_theme_toggle_named(site):
    for page, html in site.items():
        toggles = [a for t, a in scan(html) if a.get("id") == "theme-toggle"]
        assert toggles and toggles[0].get("aria-label"), page


# ------------------------------------------------- decoration -----------

def test_imgs_have_alt_and_glyphs_hidden(site):
    for page, html in site.items():
        for t, a in scan(html):
            if t == "img":
                assert "alt" in a, (page, a)
            if "hdot" in a.get("class", ""):
                assert a.get("aria-hidden") == "true", (page, a)


# ------------------------------------------------- contrast -------------

def _oklch_to_srgb(L, C, H):
    h = math.radians(H)
    a, b = C * math.cos(h), C * math.sin(h)
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    r = +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    bb = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s

    def gam(u):
        u = min(1.0, max(0.0, u))
        return 12.92 * u if u <= 0.0031308 else 1.055 * u ** (1 / 2.4) - 0.055

    return tuple(gam(u) for u in (r, g, bb))


def _parse_color(value):
    value = value.strip()
    m = re.fullmatch(r"oklch\(([\d.]+) ([\d.]+) ([\d.]+)\)", value)
    if m:
        return _oklch_to_srgb(*(float(g) for g in m.groups()))
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", value)
    if m:
        hx = m.group(1)
        return tuple(int(hx[i:i + 2], 16) / 255 for i in (0, 2, 4))
    m = re.fullmatch(r"#([0-9a-fA-F]{3})", value)
    if m:
        hx = m.group(1)
        return tuple(int(c * 2, 16) / 255 for c in hx)
    if value == "white":
        return (1.0, 1.0, 1.0)
    return None


def _ratio(fg, bg):
    def lum(rgb):
        def lin(u):
            return u / 12.92 if u <= 0.04045 else ((u + 0.055) / 1.055) ** 2.4
        r, g, b = (lin(u) for u in rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _tokens(css, dark):
    pattern = (r'\[data-theme="dark"\]\{(.*?)\}' if dark
               else r":root\{(.*?)\}")
    block = re.search(pattern, css, re.S).group(1)
    raw = dict(re.findall(r"--([\w-]+):([^;}]+)[;}]?", block))
    def resolve(name, seen=()):
        value = raw[name].strip()
        m = re.fullmatch(r"var\(--([\w-]+)\)", value)
        if m and m.group(1) not in seen:
            return resolve(m.group(1), seen + (name,))
        return value
    return {k: resolve(k) for k in raw}


TEXT_TOKENS = ["ink", "text", "muted", "text-secondary", "text-subtle",
               "green-text", "ok-text", "warn-text", "bad-text"]
UI_TOKENS = ["border-strong", "focus"]


def test_token_contrast_both_themes(site):
    css = styles(site["index.html"])
    for dark in (False, True):
        tokens = _tokens(css, dark)
        # dark block only overrides; fall back to :root for the rest
        if dark:
            base = _tokens(css, False)
            tokens = {**base, **tokens}
        theme = "dark" if dark else "light"
        backgrounds = {name: _parse_color(tokens[name])
                       for name in ("bg", "surface")}
        for token in TEXT_TOKENS:
            fg = _parse_color(tokens[token])
            assert fg is not None, (theme, token)
            for bg_name, bg in backgrounds.items():
                r = _ratio(fg, bg)
                assert r >= 4.5, (theme, token, bg_name, round(r, 2))
        for token in UI_TOKENS:
            fg = _parse_color(tokens[token])
            r = _ratio(fg, backgrounds["bg"])
            assert r >= 3.0, (theme, token, round(r, 2))
        # the filled check circles and the tier-3 chip set white on ok-fill
        r = _ratio(_parse_color("white"), _parse_color(tokens["ok-fill"]))
        assert r >= 4.5, (theme, "white on ok-fill", round(r, 2))


def test_advisory_token_contrast_both_themes(site):
    """Tokens added for the advisory surfaces (v0.2.21): severity text
    flags in the install card, severity card borders, the critical-card
    tint, and the green reassurance card's text."""
    css = styles(site["index.html"])
    base = _tokens(css, False)
    for dark in (False, True):
        tokens = {**base, **_tokens(css, True)} if dark else base
        theme = "dark" if dark else "light"
        surface = _parse_color(tokens["surface"])
        # .adv-flag text colours on the install card (sev-medium/-high use
        # warn-text/bad-text, covered above; sev-low has its own token)
        r = _ratio(_parse_color(tokens["sev-low"]), surface)
        assert r >= 4.5, (theme, "sev-low text on surface", round(r, 2))
        # severity card borders and the version-row marker: non-text, 3:1
        for token in ("amber", "red", "sev-low"):
            r = _ratio(_parse_color(tokens[token]), surface)
            assert r >= 3.0, (theme, f"{token} border on surface", round(r, 2))
        # body text on the critical-advisory tinted card
        r = _ratio(_parse_color(tokens["text"]), _parse_color(tokens["crit-bg"]))
        assert r >= 4.5, (theme, "text on crit-bg", round(r, 2))
        # the green reassurance card ("current release is not affected")
        for token in ("green-body", "green-head"):
            r = _ratio(_parse_color(tokens[token]),
                       _parse_color(tokens["green-bg"]))
            assert r >= 4.5, (theme, f"{token} on green-bg", round(r, 2))
        # .adv .id metadata line on open (surface-backed) advisory cards
        r = _ratio(_parse_color(tokens["faint-label"]), surface)
        assert r >= 4.5, (theme, "faint-label on surface", round(r, 2))


def test_advisory_pages_structure(index_dir, tmp_path):
    """The advisory permalink pages and index (v0.2.21) carry the same
    structural guarantees as the original four page types."""
    import yaml
    adv = index_dir / "advisories" / "CAMP-2026-0001.yml"
    adv.parent.mkdir(exist_ok=True)
    adv.write_text(yaml.safe_dump({
        "id": "CAMP-2026-0001", "component": "mod_example",
        "title": "Test issue", "severity": "high",
        "affected-versions": "<=1.0.0", "fixed-in": "1.1.0",
        "revoke": False, "published": "2026-07-11T00:00:00Z",
        "description": "x"}, sort_keys=False))
    out = tmp_path / "adv-site"
    site_generate(index_dir, "https://repo.test", out)

    for page in ("advisories/index.html", "advisories/CAMP-2026-0001.html",
                 "plugin/mod_example.html"):
        html = (out / page).read_text()
        elems = scan(html)
        mains = [a for t, a in elems if t == "main"]
        assert len(mains) == 1 and mains[0].get("id") == "main-content", page
        assert any(t == "a" and a.get("href") == "#main-content"
                   for t, a in elems), page
        levels = [int(t[1]) for t, _ in elems if re.fullmatch(r"h[1-6]", t)]
        assert levels and levels[0] == 1, page
        for prev, cur in zip(levels, levels[1:]):
            assert cur <= prev + 1, (page, levels)

    # the advisory card on the plugin page is a real link with the
    # severity class, and the section anchor exists for deep links
    plugin_html = (out / "plugin" / "mod_example.html").read_text()
    cards = [a for t, a in scan(plugin_html)
             if t == "a" and "adv" in a.get("class", "").split()]
    assert cards, "advisory card is not a link"
    assert all("sev-high" in a.get("class", "") for a in cards)
    assert 'id="advisories"' in plugin_html


def test_inline_hex_pairs_pass(site):
    # every inline style that sets a text colour must carry its own
    # background in the same attribute, and the pair must clear AA
    for page, html in site.items():
        for style in re.findall(r'style="([^"]*color:#[^"]*)"', html):
            colors = dict(re.findall(r"(background|color):(#[0-9a-fA-F]{3,6})",
                                     style))
            assert "color" in colors and "background" in colors, (page, style)
            r = _ratio(_parse_color(colors["color"]),
                       _parse_color(colors["background"]))
            assert r >= 4.5, (page, style, round(r, 2))


def test_fg_for_picks_the_stronger_foreground():
    for bg in ("#22c55e", "#3b82f6", "#0f766e", "#e05d44", "#555555"):
        chosen = _fg_for(bg)
        bg_rgb = _parse_color(bg)
        picked = _ratio(_parse_color("white" if chosen == "#fff" else "#111111"),
                        bg_rgb)
        other = _ratio(_parse_color("#111111" if chosen == "#fff" else "white"),
                       bg_rgb)
        assert picked >= other, bg


# ------------------------------------------------- stylesheet lint ------

# aria-hidden glyphs sized to their circles, not readable text — the
# typography floor governs text (see the "recenter the check glyphs"
# commit); everything else in the stylesheet must clear 12px
DECORATIVE_RULES = (".vpill .c", ".vline .c", ".lstep::before")


def test_no_font_size_below_the_floor(site):
    for page, html in site.items():
        for chunk in styles(html).split("}"):
            selector, _, decls = chunk.rpartition("{")
            if any(d in selector for d in DECORATIVE_RULES):
                continue
            for value, unit in re.findall(
                    r"font-size:\s*([\d.]+)(px|rem|em)", decls):
                size = float(value)
                if unit == "px":
                    assert size >= 12, (page, selector.strip(), value + unit)
                else:  # rem/em: 0.75 of the 16px root (or of any
                       # floor-passing parent) stays at or above 12px
                    assert size >= 0.75, (page, selector.strip(), value + unit)
