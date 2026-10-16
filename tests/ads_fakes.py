"""Test doubles shared by the ads test suites (same shape as FakeResponse/FakeSession in test_server.py).

Usage in a test module:
    from ads_fakes import FakeResponse, FakeSession, no_network, no_keyring  # noqa: F401  (fixtures)
`no_network` is autouse: any real requests.Session.request fails the test. `no_keyring` (opt-in, or autouse in
test_ads_auth) replaces the OS keyring with an in-memory dict, returned by the fixture."""

from __future__ import annotations

import json

import pytest
import requests

# The real method, saved before no_network patches it: lets a test drive a real Session through a fake
# transport adapter (mounted for http:// and https://, so still zero network).
REAL_SESSION_REQUEST = requests.Session.request

# Fake secrets are assembled at run time so source scanners (gitleaks, push protection) never see a token
# shape in the repository; the values built from them are obviously fake.
GOOGLE_ACCESS_PREFIX = "ya" + "29."
GOOGLE_REFRESH_PREFIX = "1" + "//"
META_TOKEN_PREFIX = "EA" + "A"
OPENAI_KEY_PREFIX = "s" + "k-"


class FakeAdapter(requests.adapters.BaseAdapter):
    """Transport adapter answering every request with one canned status and headers, never opening a socket.
    Records the prepared requests in `.sent`."""

    def __init__(self, status=302, headers=None, body=b""):
        super().__init__()
        self.status, self.headers, self.body = status, headers or {}, body
        self.sent = []

    def send(self, request, **kwargs):
        import io

        from requests.structures import CaseInsensitiveDict

        self.sent.append(request)
        response = requests.Response()
        response.status_code = self.status
        response.headers = CaseInsensitiveDict(self.headers)
        response.raw = io.BytesIO(self.body)
        response.url = request.url
        response.request = request
        response.connection = self
        response.reason = "fake"
        return response

    def close(self):
        pass


def real_session_with(monkeypatch, session, adapter):
    """Restore the real Session.request for this test and route `session` through `adapter` only."""
    monkeypatch.setattr(requests.Session, "request", REAL_SESSION_REQUEST)
    session.adapters.clear()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class FakeResponse:
    """Like requests.Response: status_code, ok, headers, text, json(), and the streaming API (iter_content,
    raw.read, close) that AdsClient uses.

    The streamed body is the JSON of `payload`, or `text` when payload is an Exception (json() raises it).
    `content` overrides the body: bytes, or an iterable of byte chunks (e.g. a generator simulating a huge
    answer: it is consumed lazily; `bytes_read` counts what was read, `closed` tells if close() was called)."""

    def __init__(self, status=200, payload=None, headers=None, text=None, content=None):
        self.headers = headers or {}
        self.status_code = status
        self.ok = status < 400
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else str(self._payload)
        self._content = content
        self.bytes_read = 0
        self.closed = False
        self.raw = _FakeRaw(self)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def _chunks(self):
        if self._content is None:
            if isinstance(self._payload, Exception):
                body = self.text.encode("utf-8")
            else:
                body = json.dumps(self._payload, default=str).encode("utf-8")
            yield body
        elif isinstance(self._content, (bytes, bytearray)):
            yield bytes(self._content)
        else:
            yield from self._content

    def iter_content(self, chunk_size=1, decode_unicode=False):
        size = chunk_size or 65536
        for chunk in self._chunks():
            for start in range(0, len(chunk), size):
                piece = chunk[start:start + size]
                self.bytes_read += len(piece)
                yield piece

    def close(self):
        self.closed = True


class _FakeRaw:
    def __init__(self, response):
        self._iter = None
        self._response = response

    def read(self, amt=None):
        if self._iter is None:
            self._iter = self._response.iter_content(chunk_size=amt or 65536)
        return next(self._iter, b"")


class FakeSession:
    """Answers requests from rules and records every call as (method, url, kwargs).

    A rule is (url_fragment, response) or (METHOD, url_fragment, response); response may be a FakeResponse,
    a callable(kwargs) returning one, or an exception instance to raise (e.g. requests.ConnectionError)."""

    def __init__(self, rules=()):
        self.rules = list(rules)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for rule in self.rules:
            rule_method, fragment, response = rule if len(rule) == 3 else (None, rule[0], rule[1])
            if rule_method and rule_method.upper() != method.upper():
                continue
            if fragment in url:
                if isinstance(response, Exception):
                    raise response
                return response(kwargs) if callable(response) else response
        return FakeResponse(404, {"error": {"message": "unexpected url " + url}})

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Zero real network: a real HTTP request anywhere fails the test."""
    def refuse(self, method, url, *args, **kwargs):
        raise AssertionError(f"real network call attempted: {method} {url}")

    monkeypatch.setattr(requests.Session, "request", refuse)


class FakeKeyring(dict):
    """{(service, user): value} with the two keyring functions the code uses."""

    def get_password(self, service, user):
        return self.get((service, user))

    def set_password(self, service, user, value):
        self[(service, user)] = value


@pytest.fixture
def no_keyring(monkeypatch):
    """Replace the OS keyring with an in-memory FakeKeyring (returned), so tests never read the real one."""
    import keyring

    store = FakeKeyring()
    monkeypatch.setattr(keyring, "get_password", store.get_password)
    monkeypatch.setattr(keyring, "set_password", store.set_password)
    return store
