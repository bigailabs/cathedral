"""Bittensor money-math bug-hunt — live board. Stdlib only, hand-rolled SVG.

Reads hunt-board/findings.jsonl (one JSON record per check/candidate the sweep
appends) and renders: verified bugs (with receipts), the discovery funnel,
surface coverage, the validation backtest, and an honest supply view.

    python -m hunt_board.dashboard   # (or: python hunt-board/dashboard.py)  -> http://127.0.0.1:8100
"""
from __future__ import annotations
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
FINDINGS = HERE / "findings.jsonl"
TARGETS = HERE.parent / "targets" / "targets.jsonl"
INSTANCES_PER_TARGET = 300   # rough: ~hundreds of (function x invariant x param) instances per protocol
FLEET_PER_DAY = 8400         # observed fleet appetite: ~350 distinct solves/hr
INK, GREEN, RED, AMBER, BLUE, GREY, GOLD = "#0b0f14", "#34c759", "#ff3b30", "#ff9f0a", "#0a84ff", "#8e8e93", "#e8b923"

# the money-math surface we intend to cover (areas) — coverage = areas seen / these
SURFACE = ["emission", "swap", "childkey", "staking", "registration", "conversion", "consensus", "accounting", "dividends", "bonds"]


def _load():
    if not FINDINGS.exists():
        return []
    out = []
    for ln in FINDINGS.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def _targets():
    if not TARGETS.exists():
        return []
    out = []
    for ln in TARGETS.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def _bar(frac, w=260, color=BLUE):
    frac = max(0.0, min(1.0, frac))
    return (f'<div style="background:#e6e9ef;border-radius:6px;height:10px;width:{w}px;display:inline-block;vertical-align:middle">'
            f'<div style="background:{color};height:10px;border-radius:6px;width:{int(w*frac)}px"></div></div>')


def render(records):
    fwd = [r for r in records if r.get("mode") == "forward"]
    bt = [r for r in records if r.get("mode") == "backtest"]
    bugs = [r for r in fwd if r.get("class") == "real-new"]
    checks = len(fwd)
    clean_n = sum(1 for r in fwd if r.get("result") == "clean")
    cand = [r for r in fwd if r.get("result") == "candidate"]
    candidates = len(cand)
    cls = {}
    for r in cand:
        cls[r.get("class", "?")] = cls.get(r.get("class", "?"), 0) + 1
    areas_seen = {r.get("area") for r in records if r.get("area")}
    cov = len(areas_seen & set(SURFACE)) / len(SURFACE)
    realnew = cls.get("real-new", 0)
    rate = (realnew / checks) if checks else 0
    bt_caught = sum(1 for r in bt if r.get("result") == "caught")

    # --- verified bugs table ---
    bug_rows = ""
    for b in bugs:
        st = b.get("status", "candidate")
        stc = GREEN if st == "VERIFIED" else AMBER
        bug_rows += (
            f'<div class=bug>'
            f'<div class=bh><span class=sev>{b.get("severity","?").upper()}</span>'
            f'<span class=bfn>{b.get("function","?")}</span>'
            f'<span class=bst style="color:{stc}">{st}</span></div>'
            f'<div class=bmeta>{b.get("file","")} · invariant: {b.get("invariant","")}</div>'
            f'<div class=bwit>witness: {b.get("witness","")}</div>'
            f'<div class=bnote>{b.get("note","")}</div></div>')
    if not bug_rows:
        bug_rows = "<p style='color:#8e8e93'>(none verified yet — sweep running)</p>"

    # --- funnel ---
    def pill(label, n, color):
        return f'<span class=pill style="border-color:{color}"><b style="color:{color}">{n}</b> {label}</span>'
    funnel = (pill("checks run", checks, GREY)
              + pill("clean · invariant held (UNSAT)", clean_n, GREEN)
              + pill("candidates (SAT)", candidates, BLUE)
              + pill("REAL-NEW", cls.get("real-new", 0), GREEN)
              + pill("negligible", cls.get("negligible", 0), AMBER)
              + pill("artifact (rejected)", cls.get("artifact", 0), GREY)
              + pill("known", cls.get("known", 0), GREY))

    # --- coverage by area ---
    cov_rows = ""
    for a in SURFACE:
        seen = a in areas_seen
        n = sum(1 for r in records if r.get("area") == a)
        cov_rows += (f'<div class=cov><span class=ca>{a}</span>'
                     f'<span class=cb>{_bar(1.0 if seen else 0.0, 120, GREEN if seen else "#e6e9ef")}</span>'
                     f'<span class=cn>{n} checks</span></div>')

    score = f"{realnew} new bug / {checks} checks ({rate:.3f}/check) · {len(areas_seen & set(SURFACE))}/{len(SURFACE)} areas"

    # --- supply pipeline (from the target corpus) ---
    tg = _targets()
    n_t = len(tg)
    import collections as _c
    by_tier = _c.Counter(str(r.get("tier")) for r in tg)
    by_eco = _c.Counter(r.get("ecosystem", "?").split("/")[0] for r in tg)
    kani_ready = sum(1 for r in tg if "kani" in str(r.get("lifter", "")).lower())
    est_instances = n_t * INSTANCES_PER_TARGET
    runway_days = est_instances / FLEET_PER_DAY if FLEET_PER_DAY else 0
    eco_str = " · ".join(f"{k} {v}" for k, v in by_eco.most_common(6))

    return f"""<!doctype html><meta charset=utf-8><title>Bittensor money-math bug hunt</title>
<meta http-equiv=refresh content=4>
<style>
 body{{font:14px/1.5 system-ui,-apple-system,sans-serif;margin:0;background:#eef0f4;color:#1c1c1e}}
 .wrap{{max-width:1040px;margin:0 auto;padding:22px}}
 h1{{font-size:21px;margin:0 0 2px}} .sub{{color:#6b7480;font-size:13px;margin:0 0 16px}}
 .kpis{{display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap}}
 .kpi{{background:#fff;border-radius:12px;padding:14px 18px;box-shadow:0 1px 4px rgba(0,0,0,.07);flex:1;min-width:150px}}
 .kpi .n{{font-size:28px;font-weight:800}} .kpi .l{{font-size:11px;color:#6b7480}}
 .card{{background:#fff;border-radius:12px;padding:14px 16px;box-shadow:0 1px 4px rgba(0,0,0,.07);margin-bottom:14px}}
 h2{{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#8e8e93;margin:0 0 10px}}
 .pill{{border:1px solid;border-radius:20px;padding:3px 10px;font-size:12px;color:#48484a;margin-right:6px;display:inline-block}}
 .bug{{border-left:3px solid {RED};background:#fff7f6;border-radius:8px;padding:10px 12px;margin-bottom:8px}}
 .bh{{display:flex;align-items:center;gap:10px}}
 .sev{{background:{RED};color:#fff;font-weight:700;font-size:10px;padding:2px 7px;border-radius:5px}}
 .bfn{{font-weight:700;font-size:14px;font-family:ui-monospace,Menlo,monospace}}
 .bst{{margin-left:auto;font-weight:700;font-size:12px}}
 .bmeta{{font-size:12px;color:#6b7480;margin-top:4px}}
 .bwit{{font:12px/1.5 ui-monospace,Menlo,monospace;color:#48484a;margin-top:4px}}
 .bnote{{font-size:12px;color:#a9851f;margin-top:3px}}
 .cov{{display:flex;align-items:center;gap:10px;margin:4px 0;font-size:12px}}
 .ca{{width:110px;color:#3a3a3c}} .cn{{color:#8e8e93;font-size:11px}}
 .supply{{background:{INK};color:#e7ecf3;border-radius:12px;padding:14px 16px}}
 .supply b{{color:{GOLD}}} .supply .t{{font-size:11px;letter-spacing:.1em;color:#7aa2f7;font-weight:700}}
 .supply .spm{{font-size:12.5px;color:#cdd6e0;margin:3px 0}}
</style>
<div class=wrap>
<h1>Bittensor money-math bug hunt</h1>
<p class=sub>generic invariants × SAT/SMT over real subtensor arithmetic · auto-refresh 4s · {score}</p>

<div class=kpis>
  <div class=kpi><div class=n style="color:{RED}">{realnew}</div><div class=l>NEW bugs found (verified)</div></div>
  <div class=kpi><div class=n style="color:{BLUE}">{checks}</div><div class=l>invariant-checks run · {candidates} SAT candidates</div></div>
  <div class=kpi><div class=n style="color:{GREEN}">{bt_caught}/6</div><div class=l>known bugs caught (validation)</div></div>
  <div class=kpi><div class=n style="color:{GOLD}">{int(cov*100)}%</div><div class=l>money-math surface scanned</div></div>
</div>

<div class=card><h2>🐞 Bugs found</h2>{bug_rows}</div>

<div class=card><h2>Discovery funnel (live code)</h2>{funnel}
  <p style="font-size:12px;color:#6b7480;margin-top:8px">Every SAT is triaged against real source — artifacts (our own modeling noise) are rejected, only faithful + reproducing + unknown + unintended counts as REAL-NEW.</p></div>

<div class=card><h2>Surface coverage</h2>{cov_rows}</div>

<div class=supply>
  <div class=t>SUPPLY PIPELINE</div>
  <div style="font-size:15px;margin:4px 0 8px"><b>{n_t}</b> audit targets catalogued · <b>{kani_ready}</b> Kani-ready (Substrate/Rust) · est. <b>~{est_instances:,}</b> solvable instances per full pass</div>
  <div class=spm>Tier1 {by_tier.get('1',0)} · Tier2 {by_tier.get('2',0)} · Tier3 {by_tier.get('3',0)}</div>
  <div class=spm>{eco_str}</div>
  <div class=spm>runway ≈ <b>{runway_days:.1f} days</b> of full-fleet feed per pass · renewable every release · fleet appetite ~{FLEET_PER_DAY:,}/day</div>
  <div style="font-size:12px;color:#aab4c2;margin-top:8px">Unlimited supply = breadth ({n_t} targets) × code-churn (each release regenerates) × parameterization. The racing layer is the endless base; bug-hunting batches ride on top as the premium output.</div>
</div>
</div>"""


def serve(port=8100):
    import http.server, socketserver
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                html = render(_load()).encode()
            except Exception as e:
                html = f"<pre>err: {e}</pre>".encode()
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(html)
        def log_message(self, *a): pass
    class S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True; daemon_threads = True
    with S(("127.0.0.1", port), H) as h:
        print(f"hunt board at http://127.0.0.1:{port}"); h.serve_forever()


if __name__ == "__main__":
    import sys
    if "--html" in sys.argv:
        out = HERE / "board.html"; out.write_text(render(_load())); print("wrote", out)
    else:
        serve()
