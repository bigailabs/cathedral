"""Provenance + growth dashboard. Stdlib only (no deps). Renders the harness
state to a clean HTML page; THE GUARD (which repo/vali on which hotkey/netuid,
broadcast mode) is the banner at the top.

    python -m scaffold.harness 8           # produce data/harness_state.json
    python -m scaffold.dashboard           # write data/dashboard.html + serve :8porta
    python -m scaffold.dashboard --html    # just write the static HTML, no server
"""
from __future__ import annotations

import json
from pathlib import Path

STATE = Path("data/harness_state.json")
OUT = Path("data/dashboard.html")


def _table(title: str, rows: list[tuple[str, str]]) -> str:
    body = "".join(f"<tr><td>{k}</td><td style='text-align:right'>{v}</td></tr>" for k, v in rows)
    return f"<h3>{title}</h3><table>{body}</table>"


def render_html(s: dict) -> str:
    prov = s.get("provenance", {})
    mode = "BROADCAST" if prov.get("broadcast_enabled") else "DRY-RUN — not broadcast"
    meta = s.get("metagraph", {})
    meta_line = (f"netuid {prov.get('netuid')} n={meta.get('n')} (live)" if meta.get("available")
                 else f"netuid {prov.get('netuid')} metagraph unavailable: {meta.get('reason','')}")
    guard = (f"<div class=guard><b>THE GUARD</b> — live validator: "
             f"<b>{prov.get('validator_label')}</b> · repo {prov.get('repo')}@{prov.get('commit')} · "
             f"hotkey <code>{prov.get('hotkey')}</code> · {meta_line} · net {prov.get('network')} · "
             f"since {prov.get('started_at_iso')} · mode <b>{mode}</b></div>")

    workers = "".join(
        f"<tr><td>{w}</td><td style='text-align:right'>{sc:.4f}</td></tr>"
        for w, sc in s.get("per_worker_score", {}).items())
    worker_tbl = (f"<h3>Per-worker cumulative score ({s.get('workers')} workers, 1 shared hotkey)</h3>"
                  f"<table><tr><th>worker-id</th><th>score</th></tr>{workers}</table>")

    growth = "".join(
        f"<tr><td>{g['round']}</td><td style='text-align:right'>{g['graded_total']}</td>"
        f"<td style='text-align:right'>{g['earning_workers']}</td></tr>"
        for g in s.get("growth", []))
    growth_tbl = (f"<h3>Growth over rounds</h3><table>"
                  f"<tr><th>round</th><th>graded total</th><th>earning workers</th></tr>{growth}</table>")

    att = s.get("attest", {})
    att_tbl = _table("Lane B live /v1/attest (capped)", [
        ("cap", str(att.get("cap"))), ("live calls", str(att.get("live_calls"))),
        ("intel-verified", str(att.get("live_verified"))), ("cost USD", f"${att.get('cost_usd')}")])

    wv = s.get("weight_vector_by_worker", {})
    wv_tbl = ("<h3>Computed weight vector (per worker) — what the vali WOULD set</h3>"
              "<table>" + "".join(f"<tr><td>{k}</td><td style='text-align:right'>{v}</td></tr>"
                                  for k, v in wv.items()) + "</table>"
              f"<p>on-chain UIDs (1 shared hotkey ⇒ ≤1 UID): "
              f"<code>{s.get('weight_vector_on_chain_uids')}</code></p>"
              f"<p>submit: <code>{json.dumps(s.get('submit_result'))}</code></p>")

    gates_tbl = _table("Gates fired (rejections)", [(k, str(v)) for k, v in s.get("gates_fired", {}).items()])
    cons_tbl = _table("Consensus flags", [(k, str(v)) for k, v in s.get("consensus_flags", {}).items()])
    lanes_tbl = _table("Per-lane throughput", [(k, str(v)) for k, v in s.get("lane_throughput", {}).items()])

    return f"""<!doctype html><meta charset=utf-8><title>Tripartite — provenance + growth</title>
<style>body{{font:14px/1.5 system-ui,monospace;margin:24px;max-width:920px;color:#111}}
.guard{{background:#fffbe6;border:2px solid #e6c200;padding:10px 14px;border-radius:8px;margin-bottom:18px}}
table{{border-collapse:collapse;margin:6px 0 18px;min-width:380px}}
td,th{{border:1px solid #ddd;padding:4px 10px}} th{{background:#f4f4f4;text-align:left}}
code{{background:#f4f4f4;padding:1px 4px;border-radius:3px}} h2{{margin-top:0}}
.cols{{display:flex;gap:28px;flex-wrap:wrap}}</style>
<h2>Cathedral tripartite — e2e harness</h2>
{guard}
<div class=cols><div>{lanes_tbl}{gates_tbl}{cons_tbl}{att_tbl}</div><div>{wv_tbl}{growth_tbl}</div></div>
{worker_tbl}
<p style='color:#666'>Rounds: {s.get('rounds')}. Dry-run unless broadcast enabled; broadcast hard-refused on netuid 39 (production).</p>"""


def write_html() -> Path:
    s = json.loads(STATE.read_text())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render_html(s))
    return OUT


def serve(port: int = 8099) -> None:
    import http.server
    import socketserver
    write_html()
    html = OUT.read_bytes()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(json.loads(STATE.read_text()) and render_html(
                json.loads(STATE.read_text())).encode())

        def log_message(self, *a):
            pass

    with socketserver.TCPServer(("127.0.0.1", port), H) as httpd:
        print(f"dashboard at http://127.0.0.1:{port}")
        httpd.serve_forever()


if __name__ == "__main__":
    import sys
    if "--html" in sys.argv:
        print("wrote", write_html())
    else:
        serve()
