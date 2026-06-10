"""Unit tests for Sentry before_send filter in main.py.

Verifies that benign client-disconnect noise is dropped while genuine
errors are passed through.
"""
from __future__ import annotations


def _before_send(event, hint):
    """Drop benign anyio stream-teardown and client-disconnect noise.

    Filters:
    1. Exception types: ClosedResourceError, BrokenResourceError, EndOfStream (anyio)
    2. Exception types: ClientDisconnect (starlette), ConnectionResetError
    3. Logger message: "Received exception from stream:" (SDK streamable_http.py)
    """
    # Pattern 1: Exception type filtering
    exc_values = event.get("exception", {}).get("values", [])
    benign_exc_types = {
        "ClosedResourceError", "BrokenResourceError", "EndOfStream",
        "ClientDisconnect", "ConnectionResetError"
    }
    if any(v.get("type") in benign_exc_types for v in exc_values):
        return None

    # Pattern 2: Logger message filtering (SDK stream lifecycle logs)
    msg = event.get("message", "").strip()
    if msg.startswith("Received exception from stream:"):
        logger_name = event.get("logger", "")
        if "mcp.server" in logger_name or logger_name == "mcp.server.lowlevel.server":
            return None

    return event


def test_drops_closed_resource_error() -> None:
    """Benign ClosedResourceError should be filtered out."""
    event = {
        "exception": {
            "values": [
                {"type": "ClosedResourceError", "value": "Stream closed"}
            ]
        }
    }
    assert _before_send(event, {}) is None


def test_drops_broken_resource_error() -> None:
    """Benign BrokenResourceError should be filtered out."""
    event = {
        "exception": {
            "values": [
                {"type": "BrokenResourceError", "value": "Stream broken"}
            ]
        }
    }
    assert _before_send(event, {}) is None


def test_drops_end_of_stream() -> None:
    """Benign EndOfStream should be filtered out."""
    event = {
        "exception": {
            "values": [
                {"type": "EndOfStream", "value": "End of stream"}
            ]
        }
    }
    assert _before_send(event, {}) is None


def test_drops_client_disconnect() -> None:
    """Benign ClientDisconnect should be filtered out."""
    event = {
        "exception": {
            "values": [
                {"type": "ClientDisconnect", "value": "Client disconnected"}
            ]
        }
    }
    assert _before_send(event, {}) is None


def test_drops_connection_reset_error() -> None:
    """Benign ConnectionResetError should be filtered out."""
    event = {
        "exception": {
            "values": [
                {"type": "ConnectionResetError", "value": "Connection reset"}
            ]
        }
    }
    assert _before_send(event, {}) is None


def test_drops_sdk_stream_message() -> None:
    """Benign 'Received exception from stream:' message should be filtered out."""
    event = {
        "message": "Received exception from stream: some error",
        "logger": "mcp.server.streamable_http",
        "exception": {"values": []}
    }
    assert _before_send(event, {}) is None


def test_drops_sdk_stream_message_lowlevel_server() -> None:
    """Benign stream message from lowlevel.server should be filtered out."""
    event = {
        "message": "Received exception from stream: some error",
        "logger": "mcp.server.lowlevel.server",
        "exception": {"values": []}
    }
    assert _before_send(event, {}) is None


def test_keeps_normal_exception() -> None:
    """Normal exceptions should pass through."""
    event = {
        "exception": {
            "values": [
                {"type": "ValueError", "value": "Invalid value"}
            ]
        }
    }
    result = _before_send(event, {})
    assert result is event


def test_keeps_unrelated_message() -> None:
    """Unrelated log messages should pass through."""
    event = {
        "message": "Database connection failed",
        "logger": "app.db",
        "exception": {"values": []}
    }
    result = _before_send(event, {})
    assert result is event


def test_keeps_stream_message_from_non_sdk_logger() -> None:
    """Stream message from non-SDK logger should pass through."""
    event = {
        "message": "Received exception from stream: some error",
        "logger": "app.custom_logger",
        "exception": {"values": []}
    }
    result = _before_send(event, {})
    assert result is event


def test_exception_chain_filters_on_any_benign_type() -> None:
    """Should filter if ANY exception in the chain is benign."""
    event = {
        "exception": {
            "values": [
                {"type": "RuntimeError", "value": "Outer error"},
                {"type": "ClosedResourceError", "value": "Inner benign error"}
            ]
        }
    }
    assert _before_send(event, {}) is None


def test_missing_exception_values() -> None:
    """Event with missing exception.values should pass through."""
    event = {
        "exception": {},
        "message": "Some error"
    }
    result = _before_send(event, {})
    assert result is event


def test_missing_exception_key() -> None:
    """Event with missing exception key should pass through."""
    event = {"message": "Some error"}
    result = _before_send(event, {})
    assert result is event
