"""Rate and redirect regressions with bounded clocks and no network access."""
from types import SimpleNamespace
import threading

import pytest
import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from smart_spider.http_client import SmartHttpClient
from smart_spider.rate_limit import RateLimiter
from smart_spider.url_policy import URLPolicy


@pytest.mark.parametrize("rate", [0.1, 0.5, 1.0, 10.0])
def test_fractional_rate_refills_at_configured_interval(rate, monkeypatch):
    elapsed = [0.0]
    waits = []

    def sleep(seconds):
        assert len(waits) < 10, "limiter did not make progress"
        waits.append(seconds)
        elapsed[0] += seconds

    monkeypatch.setattr("smart_spider.rate_limit.time.monotonic", lambda: elapsed[0])
    monkeypatch.setattr("smart_spider.rate_limit.time.sleep", sleep)
    limiter = RateLimiter(rate, capacity=1)
    limiter.acquire()
    assert elapsed[0] == 0  # One explicitly documented startup token.
    for _ in range(3):
        limiter.acquire()
    assert elapsed[0] == pytest.approx(3 / rate)


def test_fractional_rate_default_capacity_allows_progress(monkeypatch):
    elapsed = [0.0]
    calls = []

    def sleep(seconds):
        assert len(calls) < 3, "fractional rate exhausted bounded probe"
        calls.append(seconds)
        elapsed[0] += seconds

    monkeypatch.setattr("smart_spider.rate_limit.time.monotonic", lambda: elapsed[0])
    monkeypatch.setattr("smart_spider.rate_limit.time.sleep", sleep)
    limiter = RateLimiter(0.5)
    limiter.acquire()
    limiter.acquire()
    assert calls == [2.0]


@pytest.mark.parametrize("rate", [0, -1, float("nan"), float("inf"), -float("inf")])
def test_rate_must_be_finite_positive(rate):
    with pytest.raises(ValueError, match="finite and positive"):
        RateLimiter(rate)


@pytest.mark.parametrize("capacity", [0, 0.5, float("nan"), float("inf")])
def test_capacity_must_hold_one_finite_token(capacity):
    with pytest.raises(ValueError, match="finite and at least 1"):
        RateLimiter(0.5, capacity=capacity)


def test_concurrent_acquires_share_capacity(monkeypatch):
    elapsed = [0.0]
    clock_lock = threading.Lock()

    def now():
        with clock_lock:
            return elapsed[0]

    def sleep(seconds):
        with clock_lock:
            elapsed[0] += seconds

    monkeypatch.setattr("smart_spider.rate_limit.time.monotonic", now)
    monkeypatch.setattr("smart_spider.rate_limit.time.sleep", sleep)
    limiter = RateLimiter(0.5, capacity=1)
    errors = []

    def acquire():
        try:
            limiter.acquire()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=acquire, daemon=True) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert elapsed[0] >= 14.0  # Seven refill tokens at 0.5 QPS.
    assert 0 <= limiter._tokens <= 1


class RedirectAdapter(BaseAdapter):
    def __init__(self, redirects):
        self.redirects = redirects
        self.requests = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        response = requests.Response()
        response.url = request.url
        response.request = request
        response.status_code = 302 if request.url in self.redirects else 200
        response.headers = CaseInsensitiveDict()
        if response.status_code == 302:
            response.headers["Location"] = self.redirects[request.url]
        response._content = b"ok"
        response._content_consumed = True
        return response

    def close(self):
        pass


def make_client(monkeypatch, redirects):
    session = requests.Session()
    session.trust_env = False
    adapter = RedirectAdapter(redirects)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    monkeypatch.setattr("smart_spider.http_client._build_session", lambda: session)
    monkeypatch.setattr("smart_spider.http_client.time.sleep", lambda _: None)
    client = SmartHttpClient(
        rate=1000, max_retries=0, use_curl_cffi=False,
        url_policy=URLPolicy(resolve_dns=False),
    )
    return client, session, adapter


@pytest.mark.parametrize("target", [
    "https://other.example/end", "https://origin.example:444/end",
    "http://origin.example/end", "https://origin.example:0/end",
])
@pytest.mark.parametrize("method", ["get", "head"])
def test_cross_origin_strips_sensitive_headers_and_request_credentials(
    target, method, monkeypatch,
):
    start = "https://origin.example/start"
    client, session, adapter = make_client(monkeypatch, {start: target})
    session.auth = ("session-user", "session-secret")
    session.cookies.set("session-cookie", "synthetic-secret")
    session.headers["Proxy-Authorization"] = "synthetic-proxy-secret"
    headers = {
        "aUtHoRiZaTiOn": "Bearer synthetic-secret",
        "cOoKiE": "explicit=synthetic-secret",
        "pRoXy-AuThOrIzAtIoN": "synthetic-proxy-secret",
        "X-Request-ID": "visible",
    }
    getattr(client, method)(
        start, extra_headers=headers,
        auth=("request-user", "request-secret"),
        cookies={"request-cookie": "synthetic-secret"},
    )
    first, final = adapter.requests
    assert "Authorization" in first.headers
    assert "Cookie" in first.headers
    assert "Proxy-Authorization" in first.headers
    assert all(name not in final.headers for name in (
        "Authorization", "Cookie", "Proxy-Authorization",
    ))
    assert final.headers["X-Request-ID"] == "visible"
    assert headers["cOoKiE"] == "explicit=synthetic-secret"
    assert session.auth == ("session-user", "session-secret")
    assert session.cookies.get("session-cookie") == "synthetic-secret"


@pytest.mark.parametrize("target", [
    "/end", "https://origin.example:443/end", "https://ORIGIN.example/end",
])
def test_same_origin_preserves_explicit_auth_and_cookies(target, monkeypatch):
    start = "https://origin.example/start"
    client, _, adapter = make_client(monkeypatch, {start: target})
    client.get(
        start, auth=("fixture-user", "fixture-secret"),
        cookies={"fixture": "cookie"},
        extra_headers={"Proxy-Authorization": "fixture-proxy"},
    )
    first, final = adapter.requests
    for name in ("Authorization", "Cookie", "Proxy-Authorization"):
        assert final.headers[name] == first.headers[name]


def test_cross_origin_credentials_stay_stripped_when_returning(monkeypatch):
    start = "https://origin.example/start"
    foreign = "https://other.example/middle"
    final = "https://origin.example/end"
    client, session, adapter = make_client(monkeypatch, {start: foreign, foreign: final})
    session.trust_env = True
    monkeypatch.setattr("requests.sessions.get_netrc_auth", lambda _: ("netrc", "secret"))
    session.cookies.set("fixture", "secret", domain="origin.example")
    client.get(start)
    assert "Authorization" in adapter.requests[0].headers
    assert "Cookie" in adapter.requests[0].headers
    for request in adapter.requests[1:]:
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers


def test_curl_redirect_does_not_receive_requests_auth_or_original_cookies(monkeypatch):
    start = "https://origin.example/start"
    foreign = "https://other.example/middle"
    final = "https://origin.example/end"
    client, _, _ = make_client(monkeypatch, {})
    calls = []
    redirects = {start: foreign, foreign: final}

    def send(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(
            url=url, status_code=302 if url in redirects else 200,
            headers={"Location": redirects[url]} if url in redirects else {},
            close=lambda: None, raw=None,
        )

    monkeypatch.setattr(
        "smart_spider.http_client.curl_requests",
        SimpleNamespace(get=send, head=send), raising=False,
    )
    client._use_curl = True
    client.get(
        start, stream=True, auth=("fixture", "secret"), cookies={"fixture": "secret"},
        extra_headers={"Authorization": "fixture", "Cookie": "fixture=secret"},
    )
    assert calls[0][1]["auth"] == ("fixture", "secret")
    for _, kwargs in calls[1:]:
        assert "auth" not in kwargs
        assert "cookies" not in kwargs
        assert "Authorization" not in kwargs["headers"]
        assert "Cookie" not in kwargs["headers"]
