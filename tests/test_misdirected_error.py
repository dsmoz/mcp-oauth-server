"""Unit tests for `_is_misdirected_error` — HTTP 421 detection."""

import httpx

from src.gateway.upstream import _is_misdirected_error


def _http_status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://example.up.railway.app/mcp/")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError(f"{code}", request=req, response=resp)


def test_detects_421_status_error():
    assert _is_misdirected_error(_http_status_error(421))


def test_rejects_other_status_codes():
    for code in (200, 401, 404, 500, 502, 503):
        assert not _is_misdirected_error(_http_status_error(code))


def test_detects_421_inside_exception_group():
    leaf = _http_status_error(421)
    group = BaseExceptionGroup("tg", [leaf])
    assert _is_misdirected_error(group)


def test_detects_421_via_message_fallback():
    exc = RuntimeError("Client error '421 Misdirected Request' for url ...")
    assert _is_misdirected_error(exc)


def test_rejects_unrelated_runtime_error():
    assert not _is_misdirected_error(RuntimeError("connection refused"))
