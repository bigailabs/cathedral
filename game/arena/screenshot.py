r"""Headless screenshot of the live arena - the visual-first UI, captured.

`python -m game.arena.screenshot [rounds]` ticks the live server a few rounds (real
season progression), writes out/arena_live.html, and renders it to
out/arena_live.png with Edge headless. The Edge invocation is fiddly under WSL -
this module encodes the recipe that actually works (learned the hard way):

  * `--headless=new`            (old `--headless` crashes the GPU process in WSL)
  * GPU/rasterizer disabled     (`--disable-gpu --disable-software-rasterizer ...`)
  * profile dir on the C: mount (a \\wsl.localhost\ profile fails sandbox grants)
  * ABSOLUTE Windows path for `--screenshot` (a relative path -> "Access denied")

`screenshot_cmd(...)` (pure) builds the argv; `capture(...)` runs it best-effort.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

EDGE = "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
OUT = Path(__file__).resolve().parent / "out"


def screenshot_cmd(edge: str, html_win: str, png_win: str, profile_win: str,
                   *, width: int = 1500, height: int = 3400) -> list[str]:
    """Build the Edge headless argv. Pure + testable. Paths are Windows paths; the
    file:// URL uses forward slashes; `--screenshot` MUST be an absolute Win path.

    NOTE: no `--virtual-time-budget` here - the arena's setInterval timer would
    keep virtual time from ever idling, so Edge would never capture. This form
    captures static/animation-CSS pages cleanly (the live arena page)."""
    return [
        edge, "--headless=new", "--no-sandbox", "--disable-gpu",
        "--disable-gpu-compositing", "--disable-software-rasterizer",
        "--disable-dev-shm-usage", "--hide-scrollbars",
        f"--user-data-dir={profile_win}", f"--window-size={width},{height}",
        f"--screenshot={png_win}", f"file:///{html_win.replace(chr(92), '/')}",
    ]


def _winpath(p: Path) -> str:
    return subprocess.run(["wslpath", "-w", str(p)], capture_output=True,
                          text=True, timeout=10).stdout.strip()


def capture(html_path: Path, png_path: Path, *, edge: str = EDGE,
            timeout_s: float = 90.0) -> dict:
    """Render an HTML file to PNG with Edge headless. Best-effort + bounded;
    {ok, png, bytes} or {ok: False, reason}. Windows/WSL + Edge only.

    Accepts str or Path for both paths - this is a deliverable helper, so it must
    not crash on a string argument (it used to raise AttributeError on str.exists)."""
    html_path, png_path = Path(html_path), Path(png_path)
    if not Path(edge).exists():
        return {"ok": False, "reason": "edge_not_found"}
    if not html_path.exists():
        return {"ok": False, "reason": "html_missing"}
    profile = png_path.parent / f"edge_profile_{png_path.stem}_{time.monotonic_ns()}"
    profile.mkdir(parents=True, exist_ok=True)
    png_path.unlink(missing_ok=True)
    cmd = screenshot_cmd(edge, _winpath(html_path), _winpath(png_path), _winpath(profile))
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "reason": f"edge_failed:{type(e).__name__}"}
    if png_path.exists() and png_path.stat().st_size > 0:
        return {"ok": True, "png": str(png_path), "bytes": png_path.stat().st_size}
    return {"ok": False, "reason": "no_png_written"}


def screenshot_url_cmd(edge: str, url: str, png_win: str, profile_win: str,
                       *, width: int = 1500, height: int = 2200,
                       vtime_ms: int = 4500) -> list[str]:
    """Build the Edge headless argv to screenshot a LIVE URL (so JS runs first).
    `--virtual-time-budget` lets the page fetch its API + render before capture."""
    return [
        edge, "--headless=new", "--no-sandbox", "--disable-gpu",
        "--disable-gpu-compositing", "--disable-software-rasterizer",
        "--disable-dev-shm-usage", "--hide-scrollbars",
        f"--user-data-dir={profile_win}", f"--window-size={width},{height}",
        f"--virtual-time-budget={vtime_ms}", f"--screenshot={png_win}", url,
    ]


def capture_url(url: str, png_path: Path, *, edge: str = EDGE,
                timeout_s: float = 90.0, vtime_ms: int = 4500) -> dict:
    """Screenshot a live URL with Edge headless (JS-rendered). The profile + png
    MUST be on the C: mount. {ok, png, bytes} or {ok: False, reason}."""
    png_path = Path(png_path)
    if not Path(edge).exists():
        return {"ok": False, "reason": "edge_not_found"}
    profile = png_path.parent / f"edge_profile_{png_path.stem}_{time.monotonic_ns()}"
    profile.mkdir(parents=True, exist_ok=True)
    png_path.unlink(missing_ok=True)
    cmd = screenshot_url_cmd(edge, url, _winpath(png_path), _winpath(profile),
                             vtime_ms=vtime_ms)
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "reason": f"edge_failed:{type(e).__name__}"}
    if png_path.exists() and png_path.stat().st_size > 0:
        return {"ok": True, "png": str(png_path), "bytes": png_path.stat().st_size}
    return {"ok": False, "reason": "no_png_written"}


def scanner_game_url(port: int) -> str:
    """Return the URL Windows Edge should use to reach the WSL-hosted game."""
    host = "127.0.0.1"
    try:
        res = subprocess.run(
            ["bash", "-lc", "hostname -I | awk '{print $1}'"],
            capture_output=True, text=True, timeout=2,
        )
        candidate = res.stdout.strip()
        if res.returncode == 0 and candidate:
            host = candidate
    except (OSError, subprocess.TimeoutExpired):
        pass
    return f"http://{host}:{port}/game"


def shoot_scanner_game(png_path: Path | None = None, *, edge: str = EDGE) -> dict:
    """Serve the live arena + screenshot the PLAYABLE miner game (SUBNET BREAKER,
    /game) - the player-facing view, JS-rendered from the scanner API. Spins a
    short-lived HTTP server on a free port, captures, tears down."""
    import threading
    from http.server import ThreadingHTTPServer
    from .serve import ArenaServer, _handler
    png_path = png_path or (OUT / "scanner_game.png")
    OUT.mkdir(exist_ok=True)
    srv = ArenaServer(season_path=str(OUT / "shot_season.json"),
                      scanner_ledger_path=str(OUT / "shot_scanner.jsonl"))
    # Windows Edge captures the URL from outside WSL. Binding only WSL loopback
    # can produce a false "ok" screenshot of Edge's connection-error page.
    httpd = ThreadingHTTPServer(("0.0.0.0", 0), _handler(srv))
    port = httpd.server_port
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        # The server thread can take a moment to bind and render the first
        # request. Capturing immediately is flaky: Edge may race the listener
        # and produce no PNG even though the game is healthy.
        from urllib.request import urlopen
        ready = False
        for _ in range(25):
            try:
                body = urlopen(f"http://127.0.0.1:{port}/game", timeout=1).read()
                ready = b"SUBNET BREAKER" in body
                if ready:
                    break
            except OSError:
                pass
            time.sleep(0.2)
        if not ready:
            return {"ok": False, "reason": "scanner_game_server_not_ready"}

        res = capture_url(scanner_game_url(port), png_path, edge=edge)
        if res.get("ok") and int(res.get("bytes") or 0) < 80_000:
            res = dict(res)
            res["ok"] = False
            res["reason"] = "scanner_game_screenshot_too_small_or_error_page"
        return res
    finally:
        httpd.shutdown()
        httpd.server_close()


def render_live_html(rounds: int = 4, *, out: Path = OUT) -> Path:
    """Tick the live server `rounds` times (real season progression) and write the
    live arena HTML - the page the screenshot captures."""
    from .serve import ArenaServer
    out.mkdir(exist_ok=True)
    srv = ArenaServer(season_path=str(out / "shot_season.json"))
    for _ in range(max(1, rounds)):
        srv.tick()
    html = out / "arena_live.html"
    html.write_text(srv.html(), encoding="utf-8")
    return html


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    html = render_live_html(rounds)
    res = capture(html, OUT / "arena_live.png")
    if res["ok"]:
        print(f"screenshot OK: {res['png']} ({res['bytes']} bytes) - live arena, "
              f"{rounds} season rounds")
        return 0
    print(f"screenshot FAILED: {res['reason']} (html at {html})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
