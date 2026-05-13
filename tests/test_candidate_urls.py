"""Unit tests for `_candidate_urls` — the transport-aware URL fallback list."""

from src.gateway.upstream import _candidate_urls


HOST = "https://example.up.railway.app"


def test_mcp_registered_does_not_fall_back_to_sse():
    urls = _candidate_urls(f"{HOST}/mcp")
    assert all("/sse" not in u for u in urls), urls
    assert f"{HOST}/mcp/" in urls
    assert f"{HOST}/mcp" in urls


def test_mcp_with_trailing_slash_preserved_as_first():
    urls = _candidate_urls(f"{HOST}/mcp/")
    assert urls[0] == f"{HOST}/mcp/"
    assert all("/sse" not in u for u in urls), urls


def test_sse_registered_does_not_fall_back_to_mcp():
    urls = _candidate_urls(f"{HOST}/sse")
    assert all("/mcp" not in u for u in urls), urls
    assert f"{HOST}/sse" in urls


def test_unknown_suffix_probes_both_transports():
    urls = _candidate_urls(f"{HOST}/api")
    assert urls[0] == f"{HOST}/api"
    assert any("/mcp" in u for u in urls)
    assert any("/sse" in u for u in urls)


def test_candidates_are_unique():
    urls = _candidate_urls(f"{HOST}/mcp")
    assert len(urls) == len(set(urls)), urls


def test_registered_url_is_first():
    """Verbatim registered URL must always lead — preserves any explicit
    trailing slash or odd path the operator chose."""
    for suffix in ("/mcp", "/mcp/", "/sse", "/sse/", "/custom"):
        urls = _candidate_urls(f"{HOST}{suffix}")
        assert urls[0] == f"{HOST}{suffix}", (suffix, urls)
