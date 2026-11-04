"""Page-view counting: every page carries the tag, the tracker stays a fixed
copy, and the wording matches what the tag does."""

import hashlib
from pathlib import Path

import camp.site as site

ASSETS = Path(site.__file__).resolve().parent / "assets"

# Changing the tracker is a reviewed step: refresh camp/assets/app.js from the
# analytics server, read the diff, update the privacy page if the payload
# changed, then update this hash.
APP_JS_SHA256 = "ba7e56ef8ae9d10bd9d373ec3e8d7bc52e76a50ddbf91ff169494f11f5d559b3"


def test_tracker_copy_is_the_reviewed_one():
    assert hashlib.sha256((ASSETS / "app.js").read_bytes()).hexdigest() == APP_JS_SHA256


def test_licence_notice_ships_with_the_tracker():
    assert "Umami Software" in (ASSETS / "app.js.LICENSE.txt").read_text()


def test_tracker_sets_no_cookies_or_browser_storage():
    js = (ASSETS / "app.js").read_text()
    assert "cookie" not in js.lower()
    assert "setItem" not in js


def test_every_page_loads_the_local_copy_only_on_the_live_domain():
    html = site._page("t", "b")
    assert html.count("<script defer") == 1
    assert 'src="/assets/app.js"' in html
    assert 'data-domains="camp-registry.org"' in html
    assert 'data-host-url="https://static.expu.to"' in html
    assert 'data-exclude-search="true"' in html
    assert 'data-exclude-hash="true"' in html
    assert 'data-do-not-track="true"' in html


def test_privacy_page_is_linked_and_states_retention_and_operator():
    assert 'href="/privacy.html"' in site._footer()
    html = site._privacy_page()
    assert "kept until deleted" in html
    assert "Exputo" in html
    assert "—" not in site._privacy_page.__doc__ + html.split("<main", 1)[1].split("</main>")[0]


def test_no_page_claims_no_tracking_outright():
    source = Path(site.__file__).read_text()
    assert "no tracking</h3>" not in source
