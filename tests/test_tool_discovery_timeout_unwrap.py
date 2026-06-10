"""Unit tests for timeout unwrapping in fetch_tool_list — BaseExceptionGroup handling."""

import pytest
from unittest.mock import AsyncMock, patch
import httpx

from src.gateway.upstream import _walk_exceptions, fetch_tool_list, _is_dns_or_connect_error


def test_detects_timeout_in_base_exception_group():
    """Test that _walk_exceptions traverses BaseExceptionGroup.exceptions.

    This reproduces the actual anyio.fail_after behavior: when a timeout
    fires, anyio wraps the CancelledError in a BaseExceptionGroup (not
    ExceptionGroup, since CancelledError is BaseException).
    """
    # Simulate anyio.fail_after timeout: raises BaseExceptionGroup with CancelledError
    cancelled_exc = TimeoutError("Timeout exceeded")
    group = BaseExceptionGroup("tg", [cancelled_exc])

    # _walk_exceptions should find the TimeoutError buried in the group
    exceptions = list(_walk_exceptions(group))

    # Should find both the group itself and the timeout leaf
    assert len(exceptions) >= 2
    assert any(isinstance(e, TimeoutError) for e in exceptions), \
        f"TimeoutError not found in {[type(e).__name__ for e in exceptions]}"


def test_walk_exceptions_extracts_timeout_from_deep_group():
    """Test _walk_exceptions on deeply nested exception groups."""
    inner_timeout = TimeoutError("inner timeout")
    inner_group = BaseExceptionGroup("inner", [inner_timeout])
    outer_group = BaseExceptionGroup("outer", [inner_group])

    exceptions = list(_walk_exceptions(outer_group))

    # Should find timeout even when deeply nested
    timeout_found = any(isinstance(e, TimeoutError) for e in exceptions)
    assert timeout_found, f"TimeoutError not found in {[type(e).__name__ for e in exceptions]}"


def test_walk_exceptions_handles_mixed_exceptions():
    """Test _walk_exceptions with both Exception and BaseException leaves."""
    timeout = TimeoutError("timeout")
    http_error = RuntimeError("HTTP 500")
    group = BaseExceptionGroup("mixed", [timeout, http_error])

    exceptions = list(_walk_exceptions(group))

    # Should find both
    has_timeout = any(isinstance(e, TimeoutError) for e in exceptions)
    has_http = any(isinstance(e, RuntimeError) and "HTTP" in str(e) for e in exceptions)

    assert has_timeout, "TimeoutError not found"
    assert has_http, "RuntimeError not found"


@pytest.mark.asyncio
async def test_fetch_tool_list_unwraps_timeout_from_group():
    """Test that fetch_tool_list properly unwraps and surfaces timeout errors.

    Currently, this test demonstrates the problem: when _list_tools_via_url
    raises BaseExceptionGroup wrapping TimeoutError, fetch_tool_list catches
    it as a generic Exception and re-raises a RuntimeError that hides the
    timeout nature.

    After the fix, this test verifies that TimeoutError with clear message
    is raised instead.
    """
    timeout_exc = TimeoutError("Timeout exceeded")
    group = BaseExceptionGroup("tg", [timeout_exc])

    with patch("src.gateway.upstream._list_tools_via_url", side_effect=group):
        with pytest.raises(TimeoutError) as exc_info:
            await fetch_tool_list("https://example.up.railway.app/mcp")

        # After fix: message should clearly name the timeout and upstream
        msg = str(exc_info.value)
        assert "timed out" in msg.lower() or "timeout" in msg.lower(), \
            f"Timeout not clearly indicated in: {msg}"
        assert "example.up.railway.app" in msg, \
            f"Upstream URL not in message: {msg}"


def test_detects_dns_error_by_message():
    """Test that _is_dns_or_connect_error detects 'name or service not known'."""
    exc = RuntimeError("socket.gaierror(-3, 'name or service not known')")
    assert _is_dns_or_connect_error(exc)


def test_detects_connect_error_httpx():
    """Test that _is_dns_or_connect_error detects httpx.ConnectError."""
    exc = httpx.ConnectError("Failed to establish a new connection")
    assert _is_dns_or_connect_error(exc)


def test_detects_connection_refused():
    """Test that _is_dns_or_connect_error detects 'connection refused'."""
    exc = RuntimeError("Connection refused")
    assert _is_dns_or_connect_error(exc)


def test_rejects_non_dns_errors():
    """Test that _is_dns_or_connect_error rejects non-DNS errors."""
    exc = RuntimeError("HTTP 500 Internal Server Error")
    assert not _is_dns_or_connect_error(exc)


def test_detects_dns_error_in_group():
    """Test that _is_dns_or_connect_error traverses groups."""
    dns_exc = RuntimeError("name or service not known")
    group = BaseExceptionGroup("tg", [dns_exc])
    assert _is_dns_or_connect_error(group)
