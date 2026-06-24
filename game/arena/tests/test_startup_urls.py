"""The server must advertise a Windows-reachable URL, not just localhost.

Under WSL, `localhost:PORT` from a Windows browser often fails (no loopback
forwarding); the WSL IP works. The startup banner must surface that IP URL so the
arena is actually reachable, addressing real "localhost refused to connect" reports.
"""
from __future__ import annotations

from game.arena.serve import startup_urls


def test_startup_urls_always_includes_localhost():
    urls = startup_urls(8800, ip=None)
    assert urls == ["http://localhost:8800/home"]


def test_startup_urls_adds_the_lan_ip_when_available():
    urls = startup_urls(8800, ip="172.24.204.42")
    assert "http://localhost:8800/home" in urls
    assert "http://172.24.204.42:8800/home" in urls         # the Windows-reachable one


def test_startup_urls_honours_the_port():
    urls = startup_urls(9001, ip="10.0.0.5")
    assert all(":9001/home" in u for u in urls)


def test_startup_urls_autodetect_returns_at_least_localhost():
    # ip omitted -> autodetect; must never crash and always yield localhost first.
    urls = startup_urls(8800)
    assert urls and urls[0] == "http://localhost:8800/home"
    assert all(u.endswith("/home") for u in urls)
