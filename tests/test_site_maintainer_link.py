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
