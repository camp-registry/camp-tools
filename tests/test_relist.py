"""Members parked on an unknown plugin-type prefix relist as soon as the
family is established, not when their ledger record ages out."""

from camp.scan import parked_for_unknown_type, should_skip

PARKED = {
    "outcome": "needs-review",
    "detail": "unknown plugin type 'lytix'; declares dependency on listed "
              "local_lytix; family establishment review required before "
              "listing (camp-tools#16)",
    "first-seen": "2026-09-04", "last-checked": "2026-09-04",
    "component": "lytix_planner",
}
MISMATCH = {
    "outcome": "needs-review",
    "detail": "declares mod_x but repo name does not correspond; human "
              "sign-off required before listing (RFC §8)",
    "first-seen": "2026-09-04", "last-checked": "2026-09-04",
}
ESTABLISHED = {"lytix": {"parent": "local_lytix", "name": "Lytix modules"}}


def test_parked_prefix_is_read_from_the_detail():
    assert parked_for_unknown_type(PARKED) == "lytix"
    assert parked_for_unknown_type(MISMATCH) is None
    assert parked_for_unknown_type({"outcome": "exists", "detail": "unknown plugin type 'x'"}) is None


def test_parked_member_relists_once_family_is_established():
    ledger = {"o/moodle-lytix_planner": PARKED}
    # still inside the window and the family is unknown: skipped as before
    assert should_skip(ledger, "o/moodle-lytix_planner", "2026-09-05", 45) is True
    assert should_skip(ledger, "o/moodle-lytix_planner", "2026-09-05", 45, {}) is True
    # family established yesterday: re-evaluated on the very next scan
    assert should_skip(ledger, "o/moodle-lytix_planner", "2026-09-05", 45, ESTABLISHED) is False


def test_other_needs_review_records_keep_their_window():
    ledger = {"o/wrongname": MISMATCH}
    assert should_skip(ledger, "o/wrongname", "2026-09-05", 45, ESTABLISHED) is True
    # and an unrelated established family does not reopen a different prefix
    other = {"videojs": {"parent": "media_videojs", "name": "x"}}
    assert should_skip({"o/moodle-lytix_planner": PARKED},
                       "o/moodle-lytix_planner", "2026-09-05", 45, other) is True
