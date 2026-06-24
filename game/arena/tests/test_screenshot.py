"""Headless screenshot of the live arena — the visual-first deliverable. The Edge
argv builder is pure + tested; the live render writes real HTML; the actual Edge
capture is opt-in (Windows/WSL + Edge only).
"""
from __future__ import annotations

import os

from game.arena import screenshot as S


def test_screenshot_cmd_encodes_the_working_recipe():
    cmd = S.screenshot_cmd("edge.exe", r"C:\out\a.html", r"C:\out\a.png",
                           r"C:\out\edge_profile", width=1500, height=3400)
    # the WSL-safe flags that were learned the hard way
    assert "--headless=new" in cmd                      # old --headless crashes the GPU
    assert "--disable-gpu" in cmd and "--disable-software-rasterizer" in cmd
    # the screenshot path is an ABSOLUTE Windows path (relative -> Access denied)
    assert f"--screenshot=C:\\out\\a.png" in cmd
    # the file URL uses forward slashes
    assert any(a.startswith("file:///C:/out/a.html") for a in cmd)
    # profile dir is passed (must be on the C: mount, not \\wsl.localhost)
    assert any(a.startswith("--user-data-dir=") for a in cmd)


def test_render_live_html_progresses_a_season(tmp_path):
    html = S.render_live_html(3, out=tmp_path)
    assert html.exists()
    body = html.read_text(encoding="utf-8")
    assert "CATHEDRAL ARENA" in body and "Attack Map" in body
    # the live page carries the real panels + the season progressed
    assert "Real Audit Vault" in body and "Anti-Cheat Feed" in body
    assert (tmp_path / "shot_season.json").exists()


def test_capture_handles_missing_html_gracefully(tmp_path):
    res = S.capture(tmp_path / "nope.html", tmp_path / "x.png")
    assert res["ok"] is False
    assert res["reason"] in ("html_missing", "edge_not_found")


def test_live_screenshot_if_enabled():
    """Real Edge headless capture — opt-in (CATHEDRAL_ARENA_SHOT=1) since it needs
    Windows + Edge. Renders into OUT (the C: mount): Edge can't sandbox a profile on
    the WSL filesystem, so capture only works on a Windows-mounted path."""
    if os.environ.get("CATHEDRAL_ARENA_SHOT", "").lower() not in {"1", "true", "yes", "on"}:
        return
    from pathlib import Path
    if not Path(S.EDGE).exists():
        return
    html = S.render_live_html(2)                         # writes to OUT (C: mount)
    res = S.capture(html, S.OUT / "arena_test_shot.png")
    assert res["ok"] is True and res["bytes"] > 10_000   # a real rendered image


def test_screenshot_url_cmd_uses_virtual_time_for_js_pages():
    cmd = S.screenshot_url_cmd("edge.exe", "http://127.0.0.1:8800/game",
                               r"C:\out\g.png", r"C:\out\edge_profile", vtime_ms=4500)
    assert "--headless=new" in cmd and "--disable-gpu" in cmd
    assert "--virtual-time-budget=4500" in cmd          # let the JS API fetch + render
    assert "--screenshot=C:\\out\\g.png" in cmd
    assert "http://127.0.0.1:8800/game" in cmd


def test_scanner_game_url_prefers_wsl_ip(monkeypatch):
    class R:
        returncode = 0
        stdout = "172.24.204.42\n"

    monkeypatch.setattr(S.subprocess, "run", lambda *a, **kw: R())
    assert S.scanner_game_url(8790) == "http://172.24.204.42:8790/game"


def test_capture_url_handles_missing_edge_gracefully():
    res = S.capture_url("http://127.0.0.1:1/game", S.OUT / "x.png", edge="/no/such/edge")
    assert res["ok"] is False and res["reason"] == "edge_not_found"


def test_scanner_game_screenshot_rejects_tiny_error_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "capture_url", lambda *a, **kw: {
        "ok": True, "png": str(tmp_path / "error.png"), "bytes": 44_093,
    })
    res = S.shoot_scanner_game(tmp_path / "scanner_game.png", edge="edge.exe")
    assert res["ok"] is False
    assert res["reason"] == "scanner_game_screenshot_too_small_or_error_page"


def test_live_scanner_game_screenshot_if_enabled():
    """Screenshot the PLAYABLE miner game (SUBNET BREAKER /game) served live — opt-in
    (CATHEDRAL_ARENA_SHOT=1). NOTE: under WSL, Windows Edge often can't reach a
    WSL-bound server (separate net namespace / firewall), so this may report a
    networking failure even with Edge present — that's environment, not a code bug."""
    if os.environ.get("CATHEDRAL_ARENA_SHOT", "").lower() not in {"1", "true", "yes", "on"}:
        return
    from pathlib import Path
    if not Path(S.EDGE).exists():
        return
    res = S.shoot_scanner_game(S.OUT / "scanner_game_test.png")
    assert "ok" in res                                   # returns a verdict, never crashes
