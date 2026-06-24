"""The screenshot is a named per-fire deliverable, so the helper must be robust
(accept str paths without crashing) and the main E2E command must be able to
produce a self-verifying screenshot manifest. These tests exercise that contract
WITHOUT requiring Edge - capture() is driven with a no-op binary / fakes, so CI
is deterministic on any machine.
"""
from __future__ import annotations

import json
import sys

from game.arena import screenshot
from game.arena import __main__ as arena_main


def test_capture_coerces_str_paths_and_does_not_crash(tmp_path):
    # Regression: capture() used to raise AttributeError ('str' has no .exists)
    # when handed a string path. It must coerce str -> Path and report cleanly.
    edge = tmp_path / "edge.exe"; edge.write_text("x")          # an existing "edge"
    res = screenshot.capture(str(tmp_path / "missing.html"),    # str, not Path
                             str(tmp_path / "out.png"), edge=str(edge))
    assert res == {"ok": False, "reason": "html_missing"}       # clean, no exception


def test_capture_reports_no_png_when_renderer_is_a_noop(tmp_path, monkeypatch):
    # A real existing binary that writes nothing -> capture must report no_png_written
    # (not claim success). Patch _winpath so the test doesn't depend on wslpath.
    monkeypatch.setattr(screenshot, "_winpath", lambda p: str(p))
    html = tmp_path / "page.html"; html.write_text("<html><body>hi</body></html>")
    res = screenshot.capture(html, tmp_path / "shot.png", edge="/bin/true")
    assert res["ok"] is False and res["reason"] == "no_png_written"


def test_capture_edge_not_found_is_clean(tmp_path):
    res = screenshot.capture(tmp_path / "x.html", tmp_path / "x.png",
                             edge=str(tmp_path / "nope.exe"))
    assert res == {"ok": False, "reason": "edge_not_found"}


def test_screenshot_cmd_targets_the_png_and_file_url():
    cmd = screenshot.screenshot_cmd("edge", r"C:\a\arena.html", r"C:\a\arena.png",
                                    r"C:\a\prof")
    assert cmd[0] == "edge" and "--headless=new" in cmd
    assert any(a == r"--screenshot=C:\a\arena.png" for a in cmd)
    assert any(a.startswith("file:///C:/a/arena.html") for a in cmd)   # backslashes -> /
    assert not any("--virtual-time-budget" in a for a in cmd)          # would block capture


def test_main_shot_writes_a_self_verifying_manifest(tmp_path, monkeypatch):
    # --shot renders THIS round's arena.html and writes a machine-checkable manifest.
    # Fake the actual Edge render so the test is deterministic + fast.
    monkeypatch.setattr(arena_main, "OUT", tmp_path)
    captured = {}

    def fake_capture(html_path, png_path, **kw):
        captured["html"] = str(html_path)
        return {"ok": True, "png": str(png_path), "bytes": 4242}

    def fake_game_capture(png_path, **kw):
        captured["game_png"] = str(png_path)
        return {"ok": True, "png": str(png_path), "bytes": 242_424}

    monkeypatch.setattr(screenshot, "capture", fake_capture)
    monkeypatch.setattr(screenshot, "shoot_scanner_game", fake_game_capture)
    monkeypatch.setattr(sys, "argv", ["python", "1", "--shot"])
    arena_main.main()

    man = json.loads((tmp_path / "screenshot.json").read_text())
    assert man["requested"] is True and man["ok"] is True and man["bytes"] == 4242
    # the manifest records WHICH page was shot, and it's the E2E's arena.html
    assert man["captured_from"].endswith("arena.html")
    assert captured["html"].endswith("arena.html")
    game_man = json.loads((tmp_path / "scanner_game_screenshot.json").read_text())
    assert game_man["requested"] is True and game_man["ok"] is True
    assert game_man["captured_from"] == "/game"
    assert game_man["bytes"] == 242_424
    assert captured["game_png"].endswith("scanner_game.png")


def test_main_without_shot_writes_no_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(arena_main, "OUT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["python", "1"])
    arena_main.main()
    assert not (tmp_path / "screenshot.json").exists()      # opt-in only
