"""Client-IP derivation modes for rate limiting.

The un-proxied v2 origin (DNS-only, no Cloudflare) must NOT trust
client-supplied cf-connecting-ip / x-real-ip / first-x-forwarded-for, or a
miner can rotate fake IPs past the limiter or pin a rival's IP into a 429
bucket. CATHEDRAL_CLIENT_IP_MODE=railway flattens ALL x-forwarded-for entries
(across repeated header lines) and indexes CATHEDRAL_TRUSTED_PROXY_HOPS from
the right — the trusted edge's appended value — so no client-supplied value
can move the bucket. See issue #333.
"""
from __future__ import annotations

from scaffold.publisher import ratelimit


def _scope(headers: dict[str, str], *, client_ip: str = "10.0.0.9") -> dict:
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]
    return {"type": "http", "headers": raw, "client": (client_ip, 55555)}


def _scope_raw(raw_headers: list[tuple[str, str]], *, client_ip: str = "10.0.0.9") -> dict:
    """Scope allowing REPEATED header names (a dict can't express two XFF lines)."""
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in raw_headers]
    return {"type": "http", "headers": raw, "client": (client_ip, 55555)}


def test_default_mode_is_headers_and_trusts_cf_connecting_ip(monkeypatch):
    monkeypatch.delenv("CATHEDRAL_CLIENT_IP_MODE", raising=False)
    assert ratelimit._client_ip_mode() == "headers"
    ip = ratelimit._client_ip_from_scope(
        _scope({"cf-connecting-ip": "203.0.113.7", "x-forwarded-for": "1.2.3.4"}))
    assert ip == "203.0.113.7"


def test_railway_mode_ignores_spoofable_headers(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_CLIENT_IP_MODE", "railway")
    # Attacker sets every header they can; only the LAST x-forwarded-for entry
    # (appended by the Railway edge) is trusted.
    ip = ratelimit._client_ip_from_scope(_scope({
        "cf-connecting-ip": "6.6.6.6",       # spoofed
        "x-real-ip": "7.7.7.7",              # spoofed
        "x-forwarded-for": "9.9.9.9, 198.51.100.42",  # left=client-claimed, right=edge-appended
    }))
    assert ip == "198.51.100.42"


def test_railway_mode_rotating_fake_ips_collapse_to_same_bucket(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_CLIENT_IP_MODE", "railway")
    # Same real peer, attacker rotates the client-claimed prefix each request.
    peer = "198.51.100.42"
    ips = {
        ratelimit._client_ip_from_scope(
            _scope({"x-forwarded-for": f"{fake}, {peer}"}))
        for fake in ("1.1.1.1", "2.2.2.2", "3.3.3.3")
    }
    assert ips == {peer}  # cannot escape their own limit by rotating headers


def test_railway_mode_defeats_second_xff_header_line(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_CLIENT_IP_MODE", "railway")
    # THE ATTACK: client sends its OWN x-forwarded-for line; the trusted edge
    # appends the real peer as a SEPARATE header line. Reading only the first
    # line (or one line's last element) would return the attacker's value.
    # Flattening ALL lines and indexing from the right defeats it.
    ip = ratelimit._client_ip_from_scope(_scope_raw([
        ("x-forwarded-for", "6.6.6.6"),          # attacker-supplied line
        ("x-forwarded-for", "198.51.100.42"),    # edge-appended real peer
    ]))
    assert ip == "198.51.100.42"


def test_railway_mode_defeats_trailing_comma_and_padding(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_CLIENT_IP_MODE", "railway")
    # Trailing commas / empty slots must not become the trusted value.
    ip = ratelimit._client_ip_from_scope(_scope_raw([
        ("x-forwarded-for", "9.9.9.9,  , 198.51.100.42 ,"),
    ]))
    assert ip == "198.51.100.42"


def test_railway_mode_respects_trusted_proxy_hops(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_CLIENT_IP_MODE", "railway")
    monkeypatch.setenv("CATHEDRAL_TRUSTED_PROXY_HOPS", "2")
    # Two trusted edge hops append their view; index 2-from-right is the client
    # as the innermost trusted proxy saw it, not the last (outermost) hop.
    ip = ratelimit._client_ip_from_scope(_scope_raw([
        ("x-forwarded-for", "1.1.1.1, 198.51.100.42, 10.0.0.1"),
    ]))
    assert ip == "198.51.100.42"


def test_railway_mode_returns_unresolved_sentinel_when_chain_short(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_CLIENT_IP_MODE", "railway")
    # No XFF at all: we cannot derive a trustworthy client IP. Must NOT fall
    # back to the socket peer (on Railway that is the edge's internal address,
    # which would bucket the whole fleet together and could 429 everyone).
    # Return the UNRESOLVED sentinel; limiters fail open on it.
    ip = ratelimit._client_ip_from_scope(_scope({}, client_ip="192.0.2.55"))
    assert ip == ratelimit.UNRESOLVED_IP
    assert "192.0.2.55" not in ip  # never the socket peer


def test_unresolved_sentinel_fails_open_at_global_limiter(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_CLIENT_IP_MODE", "railway")
    monkeypatch.setenv("CATHEDRAL_RATELIMIT_RPM", "1")

    async def _app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = ratelimit.RateLimitMiddleware(_app)
    # Many requests with no derivable IP must NOT 429 (fail open), even past
    # the rpm=1 limit — they never share a bucket.
    import asyncio

    async def _one():
        status = {}
        async def _send(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg["status"]
        await mw({"type": "http", "path": "/x", "headers": [], "client": ("10.0.0.1", 1)},
                 lambda: None, _send)
        return status["code"]

    codes = [asyncio.get_event_loop().run_until_complete(_one()) for _ in range(5)]
    assert codes == [200] * 5


def test_socket_mode_ignores_all_headers(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_CLIENT_IP_MODE", "socket")
    ip = ratelimit._client_ip_from_scope(_scope(
        {"cf-connecting-ip": "6.6.6.6", "x-forwarded-for": "9.9.9.9"},
        client_ip="192.0.2.77"))
    assert ip == "192.0.2.77"


def test_unknown_mode_defaults_to_headers(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_CLIENT_IP_MODE", "bogus")
    assert ratelimit._client_ip_mode() == "headers"


def test_fail_open_increments_observability_counter(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_CLIENT_IP_MODE", "railway")
    before = ratelimit.unresolved_ip_count()
    ratelimit._client_ip_from_scope(_scope({}))            # no chain -> fail open
    ratelimit._client_ip_from_scope(_scope({}))
    # a resolvable request must NOT bump the counter
    ratelimit._client_ip_from_scope(_scope_raw([("x-forwarded-for", "9.9.9.9, 1.2.3.4")]))
    assert ratelimit.unresolved_ip_count() == before + 2
