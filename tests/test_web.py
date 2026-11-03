"""Reading web pages into the knowledge base, against a small site served from this computer (see conftest.webshop)."""
import json

import pytest

from faqengine import web
from faqengine.kb import load_kb


@pytest.fixture()
def local(monkeypatch):
    """Lets this test read the site on this computer (never allowed otherwise) and skips the polite pause between pages."""
    monkeypatch.setattr(web, "ALLOW_PRIVATE", True)
    monkeypatch.setattr(web, "DELAY", 0)


def test_a_page_is_read_without_its_menus_and_footer(webshop, local):
    page = web.read_page(webshop + "/faq")
    assert page.title == "Frequently asked questions" and "two year frame warranty" in page.text
    assert "Copyright" not in page.text and "Menu" not in page.text  # the footer and the menu are not content
    assert "# How long is the frame warranty?" in page.text  # headings are kept as headings
    home = web.read_page(webshop + "/")
    assert f"{webshop}/warranty" in home.links and f"{webshop}/warranty#top" not in home.links  # links are absolute, without fragments
    assert "Menu: about us" not in home.text and "Copyright" not in home.text


def test_a_site_is_read_from_one_page_and_stays_on_the_site(webshop, local):
    pages, notes = web.read_site(webshop + "/", 10)
    assert [p.url.removeprefix(webshop) for p in pages] == ["/", "/faq", "/warranty"]  # not the login page, the logo, the other site, the mail link, the duplicate or the redirect to a duplicate
    joined = " ".join(notes)
    assert "/secret" in joined and "robots.txt" in joined  # the site asked not to be read there
    assert "/empty" in joined and "JavaScript" in joined
    assert len(web.read_site(webshop + "/", 2)[0]) == 2 and len(web.read_site(webshop + "/", 1)[0]) == 1


def test_problems_are_explained_in_plain_words(webshop, local):
    for path, words in [("/missing", "not found"), ("/empty", "JavaScript"), ("/big", "larger than"), ("/pdf", "not a web page"), ("/secret", "robots.txt"),
                        ("/redirect-ftp", "not a web page")]:
        with pytest.raises(web.WebError, match=words):
            web.read_page(webshop + path)
    with pytest.raises(web.WebError, match="not found"):
        web.read_site(webshop + "/missing", 5)  # the first page has to work
    assert web.read_page(webshop + "/redirect").title == "Frequently asked questions"  # a redirect is followed
    assert web.read_page(webshop + "/plain").words > 40


def test_this_computer_and_its_network_are_never_read(webshop):
    for bad in ("127.0.0.1", "10.1.2.3", "192.168.0.1", "172.16.5.4", "169.254.169.254", "::1", "::ffff:127.0.0.1", "100.64.0.1", "0.0.0.0"):
        with pytest.raises(web.WebError, match="never read"):
            web._check_ip(bad)
    for good in ("93.184.216.34", "2606:4700::1111"):
        web._check_ip(good)
    with pytest.raises(web.WebError, match="never read"):
        web.read_page(webshop + "/faq")  # the site is on this computer, and the connection itself is refused
    with pytest.raises(web.WebError, match="never read"):
        web.fetch(webshop + "/faq")  # even when nothing checked the address beforehand
    for url, words in [("file:///etc/passwd", "http and https"), ("ftp://example.com/x", "http and https"), ("https://user:pw@example.com/", "user name"),
                       ("http://10.0.0.5/admin", "never read"), ("http://[::1]/", "never read"), ("http://localhost/", "not a web address"), ("", "address")]:
        with pytest.raises(web.WebError, match=words):
            web.normalize(url)
    assert web.normalize("example.com/faq") == "https://example.com/faq" and web.normalize("HTTP://Example.COM:80/a?b=1#c") == "http://example.com/a?b=1"


def test_a_stored_copy_reads_back_as_passages_labelled_with_the_address(webshop, local, tmp_path):
    page = web.read_page(webshop + "/faq")
    name = web.page_filename(page.url)
    assert name == web.page_filename(page.url) and name != web.page_filename(webshop + "/warranty") and name.endswith(".html")
    (tmp_path / name).write_bytes(web.page_html(page))
    kb = load_kb(tmp_path / name)
    assert kb.entries and all(e.kind == "passage" and e.source == page.url for e in kb.entries)  # labelled with where it was read, not with a file name
    assert any("two year frame warranty" in e.answer for e in kb.entries) and any(e.question == "How long is the frame warranty?" for e in kb.entries)
    assert "<script" not in web.page_html(page).decode()


def test_web_pages_in_a_package_can_be_read_again_and_a_page_that_fails_keeps_its_old_copy(webshop, local, tmp_path):
    from faqengine.improve import refresh_web

    (tmp_path / "source").mkdir()
    good, gone = web.page_filename(webshop + "/warranty"), web.page_filename(webshop + "/gone")
    (tmp_path / "source" / "web.json").write_text(json.dumps([{"file": good, "url": webshop + "/warranty"}, {"file": gone, "url": webshop + "/gone"}]))
    notes: list[str] = []
    fresh = refresh_web(tmp_path, tmp_path / "work", notes)
    assert list(fresh) == [good] and "Warranty cover" in fresh[good].read_text()
    assert len(notes) == 1 and "not found" in notes[0] and "earlier copy is kept" in notes[0]
    assert refresh_web(tmp_path / "no-package", tmp_path / "work2", []) == {}
