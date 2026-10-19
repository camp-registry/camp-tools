"""Plugin pages link the maintainer to the browse search on their handle
(camp-tools#2)."""

from camp.site import _maintainer_link


def test_github_handle_links_lowercased_query():
    html = _maintainer_link({"github": "GJB2048", "name": "Gareth Barnard"}, "Gareth Barnard")
    assert 'href="/?q=gjb2048"' in html
    assert ">Gareth Barnard</a>" in html


def test_gitlab_then_name_fallbacks():
    assert 'href="/?q=someone"' in _maintainer_link({"gitlab": "someone"}, "someone")
    html = _maintainer_link({"name": "Ada Lovelace"}, "Ada Lovelace")
    assert 'href="/?q=ada%20lovelace"' in html


def test_no_handle_renders_plain_escaped_text():
    html = _maintainer_link({}, "<maintainer>")
    assert html == "&lt;maintainer&gt;"
    assert "href" not in html


# camp-tools#77: the Maintainer row names every maintainer.

from camp.site import _maintainers_row


def test_row_names_every_maintainer_in_order():
    html = _maintainers_row([
        {"github": "Lesterhuis-Training-en-Consultancy"},
        {"github": "gemguardian"},
        {"github": "gjb2048", "name": "Gareth J Barnard"},
    ])
    first = html.index(">Lesterhuis-Training-en-Consultancy</a>")
    second = html.index(">gemguardian</a>")
    third = html.index(">Gareth J Barnard</a>")
    assert first < second < third
    assert 'href="/?q=gemguardian"' in html
    assert 'href="/?q=gjb2048"' in html
    assert ">Maintainers</span>" in html
    assert "co-maintainer" not in html


def test_single_maintainer_keeps_singular_label():
    html = _maintainers_row([{"github": "someone"}])
    assert ">Maintainer</span>" in html
    assert ">someone</a>" in html


def test_sub_line_is_kept():
    html = _maintainers_row([{"github": "a"}, {"github": "b"}],
                            '<div class="mt-sub">3 releases in the archive</div>')
    assert html.endswith('<div class="mt-sub">3 releases in the archive</div></span></div>')
