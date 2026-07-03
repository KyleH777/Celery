"""Scraper: HTML cleaning, retry behavior, and URL normalization."""

from unittest.mock import MagicMock, patch

import httpx

from app.scraper import _normalize_url, extract_clean_text, fetch_html, scrape_company_website

SAMPLE_HTML = """
<html>
  <head><title>Acme</title><style>.x { color: red; }</style></head>
  <body>
    <nav><a href="/">Home</a><a href="/pricing">Pricing</a></nav>
    <script>trackEverything();</script>
    <h1>Ship faster with Acme</h1>
    <p>Acme automates your release pipeline end to end.</p>
    <form><input name="email"><button>Subscribe</button></form>
    <h2>Trusted by 500 teams</h2>
    <footer>© Acme Inc. Privacy. Terms.</footer>
  </body>
</html>
"""


class TestExtractCleanText:
    def test_keeps_content_and_strips_boilerplate(self):
        text = extract_clean_text(SAMPLE_HTML)
        assert "Ship faster with Acme" in text
        assert "automates your release pipeline" in text
        assert "Trusted by 500 teams" in text
        assert "trackEverything" not in text
        assert "color: red" not in text
        assert "Pricing" not in text  # nav stripped
        assert "© Acme Inc" not in text  # footer stripped
        assert "Subscribe" not in text  # form stripped

    def test_empty_page_yields_empty_string(self):
        assert extract_clean_text("<html><body><div>no content tags</div></body></html>") == ""


class TestNormalizeUrl:
    def test_bare_domain_gets_https(self):
        assert _normalize_url("acme.com") == "https://acme.com"

    def test_existing_scheme_preserved(self):
        assert _normalize_url("http://acme.com") == "http://acme.com"


class TestFetchHtml:
    def _response(self, status=200, text="<html></html>"):
        response = MagicMock()
        response.status_code = status
        response.text = text
        return response

    def test_success_returns_html(self):
        with patch("app.scraper.httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.return_value = self._response()
            assert fetch_html("acme.com") == "<html></html>"

    def test_http_4xx_fails_fast_without_retry(self):
        with patch("app.scraper.httpx.Client") as mock_client:
            get = mock_client.return_value.__enter__.return_value.get
            response = self._response(status=404)
            response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "404", request=MagicMock(), response=response
            )
            get.return_value = response
            assert fetch_html("acme.com") is None
            assert get.call_count == 1

    def test_timeouts_retry_up_to_three_times(self):
        with patch("app.scraper.httpx.Client") as mock_client:
            get = mock_client.return_value.__enter__.return_value.get
            get.side_effect = httpx.TimeoutException("slow")
            assert fetch_html("acme.com") is None
            assert get.call_count == 3


class TestScrapeCompanyWebsite:
    def test_dead_site_returns_none(self):
        with patch("app.scraper.fetch_html", return_value=None):
            assert scrape_company_website("dead.io") is None

    def test_empty_content_returns_none(self):
        with patch("app.scraper.fetch_html", return_value="<html><body></body></html>"):
            assert scrape_company_website("empty.io") is None

    def test_real_page_returns_clean_text(self):
        with patch("app.scraper.fetch_html", return_value=SAMPLE_HTML):
            text = scrape_company_website("acme.com")
        assert text is not None
        assert "Ship faster with Acme" in text
