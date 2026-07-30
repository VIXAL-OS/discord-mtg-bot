"""Pins for the card-name validator's transient-fetch retries (July 31, 2026).

A single Scryfall 503 blip turned BOTH repos' card-names workflows red on
July 30 (runs 30551833137 / 30552143272; the next runs passed untouched).
The workflow's value is that red MEANS something — so transient availability
retries with backoff, while real failures (the July 29 index-shape class
surfaces as 4xx/KeyError) still fail fast.
"""
import urllib.error

import pytest

import tools.validate_card_names as v


def _http_error(code):
    return urllib.error.HTTPError("http://x", code, f"HTTP {code}", None, None)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(v, "_RETRY_DELAYS", (0, 0, 0))


def test_transient_503_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(503)
        return "RESPONSE"

    monkeypatch.setattr(v.urllib.request, "urlopen", fake_urlopen)
    assert v._urlopen_with_retries("http://x", timeout=5) == "RESPONSE"
    assert calls["n"] == 3


def test_network_error_retries(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("connection reset")
        return "RESPONSE"

    monkeypatch.setattr(v.urllib.request, "urlopen", fake_urlopen)
    assert v._urlopen_with_retries("http://x", timeout=5) == "RESPONSE"
    assert calls["n"] == 2


def test_404_fails_fast(monkeypatch):
    # A 404 here is the July 29 index-shape-change class — retrying would
    # just delay the real error message.
    calls = {"n": 0}

    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        raise _http_error(404)

    monkeypatch.setattr(v.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        v._urlopen_with_retries("http://x", timeout=5)
    assert calls["n"] == 1, "4xx must not retry"


def test_exhausted_retries_raise_the_last_error(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        raise _http_error(503)

    monkeypatch.setattr(v.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        v._urlopen_with_retries("http://x", timeout=5)
    assert calls["n"] == 4, "one initial attempt + three retries"


def test_both_fetch_sites_use_the_wrapper():
    import inspect
    src = inspect.getsource(v.ensure_bulk)
    assert "urllib.request.urlopen" not in src, \
        "a fetch site bypasses the retry wrapper"
    assert src.count("_urlopen_with_retries") == 2
