"""Product title lookup tests; every HTTP request is intercepted in memory."""

from __future__ import annotations

import io
import urllib.error
from email.message import Message
from http.client import HTTPMessage, IncompleteRead
from types import SimpleNamespace

import pytest

from app import products
from app.products import ProductLookupError, fetch_product_title

ASIN = "B0CH8DKDKN"
OTHER_ASIN = "B09XS7JWHH"
URL = f"https://www.amazon.eg/dp/{ASIN}"
HTML = '<html><title>Generic Amazon Egypt</title><span id="productTitle">Actual product</span></html>'


class FakeResponse(io.BytesIO):
    def __init__(self, body: str | bytes = HTML, *, status=200, url=URL, headers=None, chunk_size=None):
        super().__init__(body.encode("utf-8") if isinstance(body, str) else body)
        self.status = status
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=utf-8"
        for key, value in (headers or {}).items():
            del self.headers[key]
            if value is not None:
                self.headers[key] = value
        self.chunk_size = chunk_size
        self.read_sizes = []
        self.socket_timeouts = []
        self.fp = SimpleNamespace(raw=SimpleNamespace(
            _sock=SimpleNamespace(settimeout=self.socket_timeouts.append),
        ))

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def read1(self, size: int | None = -1):
        assert size is not None and size > 0, "All reads must have a finite byte limit"
        self.read_sizes.append(size)
        if self.chunk_size is not None:
            size = min(size, self.chunk_size)
        return super().read(size)


class FakeOpener:
    def __init__(self):
        self.responses = []
        self.calls = []
        self.handlers = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        assert self.responses, f"Unexpected HTTP request: {request.full_url}"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def http(monkeypatch):
    opener = FakeOpener()

    def build_opener(*handlers):
        opener.handlers.extend(handlers)
        return opener

    def no_urlopen(*args, **kwargs):
        pytest.fail("Unexpected urlopen; tests must not use real HTTP")

    monkeypatch.setattr(products.urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(products.urllib.request, "urlopen", no_urlopen)
    monkeypatch.setattr(products.time, "monotonic", lambda: 100.0)
    return opener


def test_extracts_actual_title_with_nested_spans_entities_and_whitespace(http):
    response = FakeResponse('''
        <title>Amazon.eg: Generic offer headline</title>
        <span id="productTitle" class="a-size-large">
            سماعات&nbsp; <span>Acme <span>Pro</span> &amp; Co</span>
            &#x2014; 10&#34; <b>Black</b><br> 2026
            <!-- not title text --><script>tracking()</script><style>.hidden{}</style>
        </span><span>Unrelated offer text</span>
    ''')
    http.responses.append(response)

    assert fetch_product_title(URL) == 'سماعات Acme Pro & Co — 10" Black 2026'
    assert response.closed
    assert response.read_sizes


@pytest.mark.parametrize("url", [
    URL + "?tag=someone-21&ascsubtag=source&linkCode=sl1#fragment",
    f"http://amazon.eg:80/Product-Name/gp/product/{ASIN.lower()}/ref=tracking?tag=old",
    f"https://www.amazon.eg:443/gp/aw/d/{ASIN}",
    f"https://AMAZON.EG/dp/{ASIN}?asin={ASIN.lower()}&dib=tracking-value",
])
def test_requests_only_plain_canonical_https_url(http, url):
    http.responses.append(FakeResponse())
    assert fetch_product_title(url, timeout=2.5) == "Actual product"

    request, timeout = http.calls[0]
    assert request.full_url == URL
    assert timeout == 2.5
    assert request.get_method() == "GET"
    assert request.get_header("Accept-encoding") == "identity"
    assert request.get_header("Authorization") is None
    assert "tag=" not in request.full_url
    assert any(isinstance(handler, products._NoRedirect) for handler in http.handlers)


def test_preserves_original_supported_storefront(http):
    url = f"https://www.amazon.co.uk/dp/{ASIN}"
    http.responses.append(FakeResponse(url=url))
    assert fetch_product_title(url) == "Actual product"
    assert http.calls[0][0].full_url == url


@pytest.mark.parametrize("url", [
    f"https://example.com/dp/{ASIN}",
    f"https://notamazon.eg/dp/{ASIN}",
    f"https://www.amazon.eg.evil.invalid/dp/{ASIN}",
    f"https://evilamazon.eg/dp/{ASIN}",
    f"https://amazon.eg.amazon.com/dp/{ASIN}",
    f"https://amazon.evil.amazon.eg/dp/{ASIN}",
    f"https://seller.amazon.eg/dp/{ASIN}",
    f"https://user:password@www.amazon.eg/dp/{ASIN}",
    f"https://www.amazon.eg@evil.invalid/dp/{ASIN}",
    f"https://evil.invalid@www.amazon.eg/dp/{ASIN}",
    f"https://www.amazon.eg:443@evil.invalid/dp/{ASIN}",
    f"https://www.amazon.eg:444/dp/{ASIN}",
    f"https://www.amazon.eg:invalid/dp/{ASIN}",
    f"ftp://www.amazon.eg/dp/{ASIN}",
    f"https://www.amazon.eg\\@evil.invalid/dp/{ASIN}",
    f"https://www.ama\nzon.eg/dp/{ASIN}",
    f"https://www.amazon.eg/dp/{ASIN}\r\n",
    f"https://[www.amazon.eg/dp/{ASIN}",
    "https://amzn.to/some-product",
    f"https://amzn.com/{ASIN}",
    f"https://link.amazon/dp/{ASIN}",
    "",
    None,
])
def test_rejects_invalid_hosts_and_urls_without_http(http, url):
    with pytest.raises(ProductLookupError):
        fetch_product_title(url)
    assert http.calls == []


@pytest.mark.parametrize("path", [
    "/", "/s?k=headphones", "/dp/", "/dp/BAD", "/dp/B0CH8DKDKNX",
    "/dp/B0CH8DKDK-", "/dp/B0CH8DKDKé", "/dp/%420CH8DKDKN",
    f"/s?asin={ASIN}",
    f"/dp/{ASIN}?asin={OTHER_ASIN}",
    f"/dp/{ASIN}?pd_rd_i={OTHER_ASIN}",
    f"/dp/{ASIN}?dib={OTHER_ASIN}",
    f"/dp/{ASIN}?asin={ASIN}&ASIN={OTHER_ASIN}",
])
def test_rejects_missing_bad_or_conflicting_asin_before_http(http, path):
    with pytest.raises(ProductLookupError, match="ASIN"):
        fetch_product_title("https://www.amazon.eg" + path)
    assert http.calls == []


@pytest.mark.parametrize("html", [
    "<title>Generic Amazon title is not the product</title>",
    '<h1 id="productTitle">Not a productTitle span</h1>',
    '<form action><span id>No product title</span></form>',
    '<span id="productTitle"> \n &nbsp; </span>',
    '<span id="productTitle">Unclosed title',
    '<span id="productTitle">One</span><span id="productTitle">Two</span>',
    '<script>"<span id=productTitle>Fake title</span>"</script>',
])
def test_never_falls_back_for_missing_empty_or_ambiguous_title(http, html):
    http.responses.append(FakeResponse(html))
    with pytest.raises(ProductLookupError, match="productTitle"):
        fetch_product_title(URL)


@pytest.mark.parametrize("challenge", [
    "<title>Robot Check</title>",
    "<title>Amazon CAPTCHA</title>",
    '<form action="/errors/validateCaptcha"><input name="guess"></form>',
    '<input id="captchacharacters">',
    "<p>Enter the characters you see below</p>",
    "<p>Enter the <b>characters</b> you see&nbsp;below</p>",
    "<p>Type the characters you see in this image</p>",
    "<p>Automated access to Amazon is not permitted</p>",
])
def test_captcha_is_rejected_even_with_apparent_product_title(http, challenge):
    response = FakeResponse(challenge + HTML)
    http.responses.append(response)
    with pytest.raises(ProductLookupError, match="CAPTCHA/access challenge"):
        fetch_product_title(URL)
    assert len(http.calls) == 1
    assert response.closed


def test_inline_text_is_not_given_invented_spaces_or_double_unescaped(http):
    http.responses.append(FakeResponse(
        '<SPAN ID="productTitle">Head<span>phone</span>s &amp;lt;special&amp;gt;</SPAN>'
    ))
    assert fetch_product_title(URL) == "Headphones &lt;special&gt;"


@pytest.mark.parametrize("target", [
    f"https://evil.invalid/dp/{ASIN}",
    f"https://www.amazon.eg.evil.invalid/dp/{ASIN}",
    f"https://user@www.amazon.eg/dp/{ASIN}",
    f"https://www.amazon.com/dp/{ASIN}",
    f"https://seller.amazon.eg/dp/{ASIN}",
    f"https://www.amazon.eg/dp/{OTHER_ASIN}",
    f"https://www.amazon.eg/dp/{ASIN}?asin={OTHER_ASIN}",
    f"https://www.amazon.eg/s?asin={ASIN}",
    "https://www.amazon.eg/errors/validateCaptcha",
    f"http://www.amazon.eg/dp/{ASIN}",
])
def test_validates_response_url_before_reading(http, target):
    response = FakeResponse(url=target)
    http.responses.append(response)
    with pytest.raises(ProductLookupError):
        fetch_product_title(URL)
    assert response.read_sizes == []
    assert response.closed


@pytest.mark.parametrize("target", [
    f"https://evil.invalid/dp/{ASIN}",
    f"//www.amazon.eg.evil.invalid/dp/{ASIN}",
    f"https://user@www.amazon.eg/dp/{ASIN}",
    f"https://www.amazon.ae/dp/{ASIN}",
    f"/dp/{OTHER_ASIN}",
    f"/dp/{ASIN}?asin={OTHER_ASIN}",
    f"/s?asin={ASIN}",
    "/errors/validateCaptcha",
    f"http://www.amazon.eg/dp/{ASIN}",
    f"https://www.ama\nzon.eg/dp/{ASIN}",
    "",
    None,
])
def test_rejects_redirect_target_before_following_it(http, target):
    response = FakeResponse(status=302, headers={"Location": target})
    http.responses.append(response)
    with pytest.raises(ProductLookupError):
        fetch_product_title(URL)
    assert len(http.calls) == 1
    assert response.read_sizes == []
    assert response.closed


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_follows_only_valid_same_product_redirects_with_plain_urls(http, status):
    target = f"https://amazon.eg/product-name/gp/product/{ASIN}"
    response = FakeResponse(status=status, headers={
        "Location": target + "?tag=old&ref=tracking#details",
    })
    http.responses.extend([response, FakeResponse(url=target)])
    assert fetch_product_title(URL) == "Actual product"
    assert [request.full_url for request, _ in http.calls] == [URL, target]
    assert response.read_sizes == []
    assert response.closed


@pytest.mark.parametrize("location", [
    f"https://www.amazon.eg/Actual-Product/dp/{ASIN}?tag=old&ascsubtag=source#details",
    f"/Actual-Product/dp/{ASIN}?tag=old&ascsubtag=source#details",
    f"//www.amazon.eg/Actual-Product/dp/{ASIN}?tag=old&ascsubtag=source#details",
])
def test_canonical_to_slugged_product_redirect_preserves_path_without_tracking(http, location):
    slugged_url = f"https://www.amazon.eg/Actual-Product/dp/{ASIN}"
    redirect = FakeResponse(status=302, headers={"Location": location})
    product = FakeResponse(url=slugged_url)
    http.responses.extend([redirect, product])

    assert fetch_product_title(URL + "?tag=source#fragment") == "Actual product"
    assert [request.full_url for request, _ in http.calls] == [URL, slugged_url]
    assert redirect.read_sizes == []
    assert product.read_sizes
    assert redirect.closed and product.closed


def test_handles_urllib_http_error_for_valid_redirect(http):
    headers = Message()
    headers["Location"] = f"/gp/product/{ASIN}?tag=old"
    body = io.BytesIO(b"Do not read the redirect body")
    redirect = urllib.error.HTTPError(URL, 302, "Found", headers, body)
    target = f"https://www.amazon.eg/gp/product/{ASIN}"
    http.responses.extend([redirect, FakeResponse(url=target)])
    assert fetch_product_title(URL) == "Actual product"
    assert [request.full_url for request, _ in http.calls] == [URL, target]
    assert body.closed


def test_disables_urllib_automatic_redirects():
    handler = products._NoRedirect()
    request = products.urllib.request.Request(URL)
    with io.BytesIO() as body:
        assert handler.redirect_request(
            request, body, 302, "Found", HTTPMessage(), "https://evil.invalid",
        ) is None


def test_redirect_loop_is_bounded(http):
    responses = [FakeResponse(status=302, headers={"Location": URL})
                 for _ in range(products._MAX_REDIRECTS + 1)]
    http.responses.extend(responses)
    with pytest.raises(ProductLookupError, match="Too many.*redirects"):
        fetch_product_title(URL)
    assert len(http.calls) == products._MAX_REDIRECTS + 1
    assert all(response.closed and not response.read_sizes for response in responses)


@pytest.mark.parametrize("status", [201, 204, 206, 300, 304, 400, 403, 404, 429, 500, 503])
def test_http_failure_is_explicit_and_body_is_not_read(http, status):
    response = FakeResponse(status=status)
    http.responses.append(response)
    with pytest.raises(ProductLookupError, match=f"HTTP {status}"):
        fetch_product_title(URL)
    assert response.closed
    assert not response.read_sizes


def test_urllib_http_error_is_wrapped_and_closed(http):
    body = io.BytesIO(b"Access denied")
    http.responses.append(urllib.error.HTTPError(URL, 403, "Forbidden", Message(), body))
    with pytest.raises(ProductLookupError, match="HTTP 403"):
        fetch_product_title(URL)
    assert body.closed


@pytest.mark.parametrize("headers, message", [
    ({"Content-Type": "application/json"}, "not HTML"),
    ({"Content-Type": None}, "not HTML"),
    ({"Content-Encoding": "gzip"}, "Content-Encoding"),
    ({"Content-Length": "not-a-number"}, "Content-Length"),
    ({"Content-Length": "-1"}, "byte limit"),
    ({"Content-Length": "1000000000"}, "byte limit"),
])
def test_rejects_invalid_response_headers_before_reading(http, headers, message):
    response = FakeResponse(headers=headers)
    http.responses.append(response)
    with pytest.raises(ProductLookupError, match=message):
        fetch_product_title(URL)
    assert response.closed
    assert response.read_sizes == []


def test_bounds_body_even_without_content_length_or_with_small_declared_length(http, monkeypatch):
    monkeypatch.setattr(products, "_MAX_RESPONSE_BYTES", 128)
    for headers in ({}, {"Content-Length": "1"}):
        response = FakeResponse(HTML + "x" * 256, headers=headers, chunk_size=17)
        http.responses.append(response)
        with pytest.raises(ProductLookupError, match="byte limit"):
            fetch_product_title(URL)
        assert response.closed
        assert sum(min(size, 17) for size in response.read_sizes) == 129


def test_exact_byte_limit_and_content_length_are_accepted(http, monkeypatch):
    body = HTML.encode()
    monkeypatch.setattr(products, "_MAX_RESPONSE_BYTES", len(body))
    response = FakeResponse(body, headers={"Content-Length": str(len(body))})
    http.responses.append(response)
    assert fetch_product_title(URL) == "Actual product"
    assert response.read_sizes[-1] == 1


def test_truncated_response_does_not_return_partial_title(http):
    http.responses.append(FakeResponse(headers={"Content-Length": str(len(HTML) + 10)}))
    with pytest.raises(ProductLookupError, match="Incomplete product response"):
        fetch_product_title(URL)


@pytest.mark.parametrize("body, content_type", [
    ('<span id="productTitle">Caf\xe9 &amp; Co</span>'.encode("latin-1"), "text/html; charset=iso-8859-1"),
    (b'<span id="productTitle">Caf&#233; &amp; Co</span>', "application/xhtml+xml"),
])
def test_respects_declared_charset_and_html_entities(http, body, content_type):
    http.responses.append(FakeResponse(body, headers={"Content-Type": content_type}))
    assert fetch_product_title(URL) == "Café & Co"


@pytest.mark.parametrize("body, content_type", [
    (HTML.encode(), "text/html; charset=unknown-encoding"),
    (b'<span id="productTitle">\xff</span>', "text/html; charset=utf-8"),
])
def test_bad_character_encoding_fails_clearly(http, body, content_type):
    http.responses.append(FakeResponse(body, headers={"Content-Type": content_type}))
    with pytest.raises(ProductLookupError, match="encoding"):
        fetch_product_title(URL)


@pytest.mark.parametrize("error", [
    urllib.error.URLError("DNS resolution failed"),
    TimeoutError("timed out"),
    ConnectionError("Connection reset"),
    IncompleteRead(b"partial"),
])
def test_request_errors_are_wrapped_without_fallback(http, error):
    http.responses.append(error)
    with pytest.raises(ProductLookupError, match="network or protocol") as excinfo:
        fetch_product_title(URL)
    assert excinfo.value.__cause__ is error


def test_read_error_is_wrapped_and_response_closed(http, monkeypatch):
    response = FakeResponse()

    def read_error(size):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(response, "read1", read_error)
    http.responses.append(response)
    with pytest.raises(ProductLookupError, match="network or protocol"):
        fetch_product_title(URL)
    assert response.closed


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, None, "10"])
def test_rejects_invalid_timeout_before_http(http, timeout):
    with pytest.raises(ProductLookupError, match="Timeout"):
        fetch_product_title(URL, timeout=timeout)
    assert http.calls == []


def test_deadline_is_shared_across_redirects_and_body_reads(http, monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(products.time, "monotonic", lambda: clock.now)
    redirect = FakeResponse(status=302, headers={"Location": URL})
    response = FakeResponse()
    http.responses.extend([redirect, response])
    original_open = http.open

    def slow_open(request, *, timeout):
        result = original_open(request, timeout=timeout)
        clock.now += 0.25
        return result

    monkeypatch.setattr(http, "open", slow_open)
    assert fetch_product_title(URL, timeout=1.0) == "Actual product"
    assert [timeout for _, timeout in http.calls] == [1.0, 0.75]
    assert response.socket_timeouts == [0.5, 0.5]


def test_slow_stream_exhausts_total_deadline(http, monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(products.time, "monotonic", lambda: clock.now)
    response = FakeResponse(chunk_size=1)
    original_read = response.read1

    def slow_read(size):
        clock.now += 0.4
        return original_read(size)

    monkeypatch.setattr(response, "read1", slow_read)
    http.responses.append(response)
    with pytest.raises(ProductLookupError, match="timed out"):
        fetch_product_title(URL, timeout=1.0)
    assert len(response.read_sizes) == 3
    assert response.socket_timeouts == pytest.approx([1.0, 0.6, 0.2])
    assert response.closed


def test_open_that_exhausts_deadline_does_not_read_body(http, monkeypatch):
    response = FakeResponse()
    http.responses.append(response)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(products.time, "monotonic", lambda: clock.now)
    original_open = http.open

    def slow_open(request, *, timeout):
        result = original_open(request, timeout=timeout)
        clock.now += 2.0
        return result

    monkeypatch.setattr(http, "open", slow_open)
    with pytest.raises(ProductLookupError, match="timed out"):
        fetch_product_title(URL, timeout=1.0)
    assert response.read_sizes == []
    assert response.closed


def test_failures_are_not_cached(http):
    http.responses.extend([FakeResponse("<title>Unavailable</title>"), FakeResponse()])
    with pytest.raises(ProductLookupError):
        fetch_product_title(URL)
    assert fetch_product_title(URL) == "Actual product"
    assert len(http.calls) == 2
