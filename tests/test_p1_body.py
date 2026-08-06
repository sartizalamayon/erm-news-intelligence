"""app.sources._body — generic article-body extraction, the shared
`fetch_article()` implementation ported from `backend/article_fetch.py`
(legacy reference, READ-ONLY). No real sockets: `fetch_generic_article_body`
is exercised against `FakeSession`.
"""

from __future__ import annotations

from app.sources._body import MAX_CHARS, extract_readable_text, fetch_generic_article_body
from tests.test_p1_helpers import FakeResponse, FakeSession


class TestExtractReadableText:
    def test_prefers_article_container_text(self):
        html = """
        <html><body>
        <nav><p>Home | About | Contact</p></nav>
        <article>
          <p>This is the real story, told across several sentences of body text.</p>
          <p>And a second paragraph continuing the real article content here.</p>
        </article>
        <footer><p>Copyright 2026</p></footer>
        </body></html>
        """
        text = extract_readable_text(html)
        assert "real story" in text
        assert "second paragraph" in text
        assert "Home | About" not in text
        assert "Copyright" not in text

    def test_falls_back_to_long_paragraphs_when_no_article_tag(self):
        html = """
        <html><body>
        <div><p>short</p></div>
        <div><p>This is a sufficiently long paragraph with real content in it, over forty chars.</p></div>
        </body></html>
        """
        text = extract_readable_text(html)
        assert "sufficiently long paragraph" in text
        assert text.strip() != "short"

    def test_drops_script_and_style_content(self):
        html = """
        <html><body>
        <script>var x = "should not appear anywhere in the output";</script>
        <style>.x { color: red; }</style>
        <article><p>Genuine article body text that should survive extraction intact.</p></article>
        </body></html>
        """
        text = extract_readable_text(html)
        assert "should not appear" not in text
        assert "Genuine article body" in text

    def test_caps_at_max_chars(self):
        long_para = "word " * 2000
        html = f"<article><p>{long_para}</p></article>"
        text = extract_readable_text(html)
        assert len(text) <= MAX_CHARS

    def test_empty_html_returns_empty_string(self):
        assert extract_readable_text("") == ""

    def test_malformed_html_never_raises(self):
        assert isinstance(extract_readable_text("<article><p>unterminated"), str)
        assert isinstance(extract_readable_text("<<<not html at all>>>"), str)


class TestFetchGenericArticleBody:
    def test_returns_text_on_success(self):
        html = "<article><p>Full article body content goes here for the test case.</p></article>"
        session = FakeSession().add("https://x.test/article", FakeResponse(status_code=200, text=html))
        body = fetch_generic_article_body(session, "https://x.test/article", source="test")
        assert "Full article body" in body

    def test_returns_empty_string_on_non_200(self):
        session = FakeSession().add("https://x.test/gone", FakeResponse(status_code=404, text=""))
        body = fetch_generic_article_body(session, "https://x.test/gone", source="test")
        assert body == ""

    def test_returns_empty_string_on_network_failure(self, monkeypatch):
        monkeypatch.setattr("app.sources._body.get", lambda *a, **k: None)
        session = FakeSession()
        body = fetch_generic_article_body(session, "https://x.test/anything", source="test")
        assert body == ""
