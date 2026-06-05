"""Cathedral tripartite — the one-page wonder. Stdlib only (hand-rolled SVG).

Not a chart wall. It shows intelligence in practice: a real smart-contract
property compiled to logic and validated with SAT, inside a three-cornered
fortress (the three lanes) whose walls are the checks that keep cheaters in —
"no escapees". Then the live worked specimen: the actual conditionals + the
witness as proof.

    python -m scaffold.live          # runner (writes data/harness_state.json + events)
    python -m scaffold.dashboard     # serve http://127.0.0.1:8099
"""
from __future__ import annotations

import json
from pathlib import Path

STATE = Path("data/harness_state.json")
EVENTS = Path("data/events.jsonl")
OUT = Path("data/dashboard.html")

GREEN, RED, GREY, BLUE, AMBER, GOLD = "#34c759", "#ff3b30", "#8e8e93", "#0a84ff", "#ff9f0a", "#e8b923"
INK = "#0b0f14"

# (family, corner-label, lane-name, one-line role, vertex x,y, accent)
TOWERS = [
    ("encoding_v1", "C", "ENCODE", "contract → logic", 360, 70, AMBER),
    ("sat_challenge_v1", "A", "SOLVE", "race to a proven answer", 120, 430, BLUE),
    ("solver_docker_v1", "B", "IMPROVE", "attested, faster", 600, 430, GREEN),
]


def _tower(x, y, label, name, role, stat, accent):
    merlons = "".join(f'<rect x={x-26+i*13:.0f} y={y-34:.0f} width=8 height=10 fill="{accent}"/>'
                      for i in range(5))
    return (
        f'{merlons}'
        f'<rect x={x-28:.0f} y={y-26:.0f} width=56 height=52 rx=5 fill="{INK}" stroke="{accent}" stroke-width=2/>'
        f'<text x={x:.0f} y={y+2:.0f} font-size=20 font-weight=700 fill="{accent}" text-anchor=middle>{label}</text>'
        f'<text x={x:.0f} y={y+44:.0f} font-size=13 font-weight=700 fill="#e7ecf3" text-anchor=middle>{name}</text>'
        f'<text x={x:.0f} y={y+60:.0f} font-size=10.5 fill="#8a93a0" text-anchor=middle>{role}</text>'
        f'<text x={x:.0f} y={y+77:.0f} font-size=11 fill="{accent}" text-anchor=middle>{stat}</text>')


def _fortress(rails: dict, paid: int, attempts: int, escaped: int) -> str:
    pts = " ".join(f"{x},{y}" for _, _, _, _, x, y, _ in TOWERS)
    walls = (f'<polygon points="{pts}" fill="rgba(11,15,20,0.04)" '
             f'stroke="{INK}" stroke-width=2 stroke-linejoin=round/>')
    # wall labels = the checks/balances that keep cheaters in
    guards = [
        (240, 250, "verify the artifact"),      # C–A edge
        (480, 250, "attest the run"),            # C–B edge
        (360, 442, "cross-miner consensus"),     # A–B edge
    ]
    gtxt = "".join(
        f'<text x={x} y={y} font-size=10 fill="#6b7480" text-anchor=middle '
        f'transform="rotate({ -56 if x<360 else (56 if x>360 else 0) } {x} {y})">⛓ {t}</text>'
        for x, y, t in guards)
    towers = "".join(
        _tower(x, y, lbl, nm, role,
               f"{int(rails.get(fam,{}).get('finds',0))} solved · {int(rails.get(fam,{}).get('blocked',0))} blocked",
               acc)
        for fam, lbl, nm, role, x, y, acc in TOWERS)
    # central vault
    vault = (
        f'<rect x=300 y=250 width=120 height=70 rx=8 fill="{INK}" stroke="{GOLD}" stroke-width=2/>'
        f'<text x=360 y=276 font-size=11 fill="{GOLD}" text-anchor=middle font-weight=700>VAULT</text>'
        f'<text x=360 y=296 font-size=18 fill="#fff" text-anchor=middle font-weight=700>{paid}</text>'
        f'<text x=360 y=311 font-size=9.5 fill="#8a93a0" text-anchor=middle>verified artifacts paid</text>')
    breached = escaped > 0
    seal_c = RED if breached else GREEN
    seal = (
        f'<circle cx=360 cy=388 r=30 fill="none" stroke="{seal_c}" stroke-width=3/>'
        f'<text x=360 y=384 font-size=9 fill="{seal_c}" text-anchor=middle font-weight=700>'
        f'{"BREACH" if breached else "SEALED"}</text>'
        f'<text x=360 y=398 font-size=15 fill="{seal_c}" text-anchor=middle font-weight=700>{escaped}</text>'
        f'<text x=360 y=410 font-size=8 fill="#8a93a0" text-anchor=middle>escaped</text>')
    cap = (f'<text x=360 y=505 font-size=11 fill="#6b7480" text-anchor=middle>'
           f'{attempts} cheat attempts tried the walls · {escaped} got out</text>')
    return (f'<svg viewBox="0 0 720 520" width="100%" style="max-width:720px">'
            f'{walls}{gtxt}{vault}{seal}{towers}{cap}</svg>')


def _specimen(sp: dict, accent: str) -> str:
    if not sp:
        return ""
    logic = "".join(f"<div class=ln>{l}</div>" for l in sp.get("logic", []))
    proof = "".join(f"<div class=pf>{p}</div>" for p in sp.get("proof", []))
    sat = "sat" in sp.get("verdict", "").lower() and "unsat" not in sp.get("verdict", "").lower()
    vcls = "v-ok" if sat else "v-neutral"
    proof_block = f"<div class=proof><div class=ph>proof</div>{proof}</div>" if proof else ""
    return (
        f'<div class=spec style="border-top:3px solid {accent}">'
        f'<div class=sh><span class=sr style="color:{accent}">{sp.get("rail","")}</span>'
        f'<span class=sc>{sp.get("contract","")}</span></div>'
        f'<div class=sp>{sp.get("property","")}</div>'
        f'<div class=code>{logic}</div>'
        f'<div class="verdict {vcls}">{sp.get("verdict","")}</div>'
        f'{proof_block}'
        f'<div class=params>{sp.get("params","")}</div>'
        f'</div>')


def _feed(n=22) -> str:
    if not EVENTS.exists():
        return "<p style='color:#6b7480'>(no events yet)</p>"
    rows = []
    for ln in reversed(EVENTS.read_text().splitlines()[-n:]):
        try:
            e = json.loads(ln)
        except Exception:
            continue
        t = e.get("type"); ts = f"<span class=ts>{e.get('ts','')}</span>"
        lane = f"<span class=ln2>{e.get('lane','')}</span>" if e.get("lane") else ""
        w = e.get("worker", "")
        if t == "mint":
            rows.append(f"<div class=ev>{ts}<span class='tg b'>MINT</span>{lane} {e.get('msg')}</div>")
        elif t == "verify":
            oc, sc = e.get("outcome", "?"), e.get("score", 0)
            cls = "ok" if sc and sc > 0 else ("bad" if oc == "invalid" else "warn")
            rows.append(f"<div class=ev>{ts}<span class='tg {cls}'>{oc.upper()}</span>{lane} <b>{w}</b> → {sc}</div>")
        elif t == "consensus":
            cls = "bad" if e.get("flag") == "outlier" else "ok"
            rows.append(f"<div class=ev>{ts}<span class='tg {cls}'>JUDGE</span>{lane} <b>{w}</b> {e.get('msg')}</div>")
        elif t == "cheat":
            rows.append(f"<div class=ev>{ts}<span class='tg bad'>ESCAPE</span>{lane} {e.get('msg')}</div>")
    return f"<div class=feed>{''.join(rows)}</div>"


def render_html(s: dict) -> str:
    rails = s.get("rails", {})
    paid = sum(int(r.get("finds", 0)) for r in rails.values())
    attempts = int(s.get("learnings", {}).get("cheat", {}).get("attempts", 0))
    escaped = int(s.get("learnings", {}).get("cheat", {}).get("bypass", 0))
    fortress = _fortress(rails, paid, attempts, escaped)
    sp = s.get("specimens", {})
    accent = {"encode": AMBER, "solve": BLUE, "improve": GREEN}
    specs = "".join(_specimen(sp.get(k), accent[k]) for k in ("encode", "solve", "improve") if sp.get(k))
    prov = s.get("provenance", {})
    foot = (f"validator {prov.get('validator_label','—')} · {prov.get('network','—')} · "
            f"{s.get('rounds',0)} rounds · pays only independently-verified artifacts")
    return f"""<!doctype html><meta charset=utf-8><title>Cathedral — intelligence in practice</title>
<meta http-equiv=refresh content=3>
<style>
 body{{font:14px/1.5 system-ui,-apple-system,sans-serif;margin:0;background:#eef0f4;color:#1c1c1e}}
 .wrap{{max-width:1080px;margin:0 auto;padding:22px}}
 h1{{font-size:21px;margin:0 0 2px}} .sub{{color:#6b7480;margin:0 0 16px;font-size:13px}}
 .hero{{display:grid;grid-template-columns:minmax(360px,1.1fr) 1fr;gap:18px;align-items:center;
        background:#fff;border-radius:16px;padding:18px 20px;box-shadow:0 1px 4px rgba(0,0,0,.07)}}
 .pitch .big{{font-size:17px;font-weight:700;line-height:1.35}}
 .pitch p{{font-size:13px;color:#48484a;margin:10px 0 0}}
 .pitch .kpis{{margin-top:14px;display:flex;gap:10px;flex-wrap:wrap}}
 .kpi{{background:#f4f6fa;border-radius:9px;padding:7px 12px}}
 .kpi b{{font-size:17px}} .kpi span{{display:block;font-size:10.5px;color:#6b7480}}
 h2{{font-size:12px;letter-spacing:.07em;text-transform:uppercase;color:#8e8e93;margin:24px 0 10px}}
 .specs{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}}
 .spec{{background:#fff;border-radius:13px;padding:14px 15px;box-shadow:0 1px 4px rgba(0,0,0,.07)}}
 .sh{{display:flex;align-items:baseline;gap:8px;margin-bottom:6px}}
 .sr{{font-weight:700;font-size:14px;letter-spacing:.03em}} .sc{{font-size:11px;color:#6b7480}}
 .sp{{font-size:12px;color:#3a3a3c;margin-bottom:9px;line-height:1.4}}
 .code{{background:{INK};border-radius:9px;padding:10px 11px;font:11px/1.6 ui-monospace,Menlo,monospace;
        color:#cdd6e0;overflow-x:auto;white-space:pre}}
 .code .ln{{white-space:pre}}
 .verdict{{margin-top:9px;font-size:12px;font-weight:700;padding:6px 9px;border-radius:7px}}
 .v-ok{{background:#e7f8ee;color:#1a8a44}} .v-neutral{{background:#f4f6fa;color:#48484a}}
 .proof{{margin-top:8px;background:#fbf7ec;border:1px solid #ecd9a6;border-radius:8px;padding:8px 10px}}
 .proof .ph{{font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;color:#a9851f;font-weight:700}}
 .proof .pf{{font:11px/1.6 ui-monospace,Menlo,monospace;color:#5a4a1a;white-space:pre-wrap;word-break:break-all}}
 .params{{margin-top:8px;font-size:10.5px;color:#8e8e93}}
 .feedwrap{{background:#fff;border-radius:13px;padding:12px 14px;box-shadow:0 1px 4px rgba(0,0,0,.07)}}
 .feed{{font:11.5px/1.7 ui-monospace,Menlo,monospace;max-height:200px;overflow-y:auto;
        background:{INK};color:#cdd6e0;border-radius:9px;padding:9px 11px}}
 .ev{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
 .ev .ts{{color:#566;margin-right:7px}} .ev .ln2{{color:#7aa2f7;margin:0 6px}}
 .tg{{display:inline-block;min-width:62px;text-align:center;padding:0 6px;border-radius:4px;margin-right:7px;font-weight:700;font-size:9.5px}}
 .tg.ok{{background:#173d2a;color:{GREEN}}} .tg.bad{{background:#3d1717;color:#ff6b60}}
 .tg.warn{{background:#3d3417;color:{AMBER}}} .tg.b{{background:#15294d;color:#7aa2f7}}
 .foot{{margin-top:16px;font-size:11px;color:#8e8e93;text-align:center}}
</style>
<div class=wrap>
<h1>Cathedral — intelligence in practice</h1>
<p class=sub>a real smart contract, compiled to logic, validated with SAT — inside a fortress no cheat escapes</p>
<div class=hero>
  <div class=fortress>{fortress}</div>
  <div class=pitch>
    <div class=big>We take a live smart-contract property, turn it into logic, and <b>prove</b> it with a solver.</div>
    <p>Three corners, three lanes — <b>Encode</b> a property into a problem, <b>Solve</b> it in an open race,
       <b>Improve</b> with attested speed. The walls are the checks (verify · attest · consensus); the vault
       pays only artifacts that survive them. Nothing unverified gets out.</p>
    <div class=kpis>
      <div class=kpi><b style="color:{GREEN}">{paid}</b><span>verified & paid</span></div>
      <div class=kpi><b style="color:{RED}">{attempts}</b><span>cheats attempted</span></div>
      <div class=kpi><b style="color:{GREEN if escaped==0 else RED}">{escaped}</b><span>escaped</span></div>
    </div>
  </div>
</div>
<h2>Intelligence in practice — live worked examples (real challenges, this minute)</h2>
<div class=specs>{specs}</div>
<h2>Live pulse</h2>
<div class=feedwrap>{_feed()}</div>
<div class=foot>{foot}</div>
</div>"""


def write_html() -> Path:
    s = json.loads(STATE.read_text())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render_html(s))
    return OUT


def serve(port: int = 8099) -> None:
    import http.server
    import socketserver

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                html = render_html(json.loads(STATE.read_text())).encode()
            except Exception as e:
                html = f"<pre>no state yet: {e}</pre>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *a):
            pass

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server(("127.0.0.1", port), H) as httpd:
        print(f"dashboard at http://127.0.0.1:{port}")
        httpd.serve_forever()


if __name__ == "__main__":
    import sys
    if "--html" in sys.argv:
        print("wrote", write_html())
    else:
        serve()
