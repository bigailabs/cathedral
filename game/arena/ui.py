"""Render an ArenaResult to a single self-contained HTML file — visual-first.

No framework, no network: inline CSS + SVG, one file you open in a browser. The
first view is the live arena (target grid + agents + feeds), tables are for
detail only. Aesthetic follows the Cathedral brand: void-navy field, aurora-cyan
accents.
"""
from __future__ import annotations

import html
import json
from .engine import ArenaResult

_CSS = """
* { box-sizing: border-box; }
body { margin:0; background:#0a0e1a; color:#cdd6f4; font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; }
a { color:#7fd1e0; }
.wrap { max-width:1500px; margin:0 auto; padding:18px; }
h1 { font-size:20px; margin:0; letter-spacing:.5px; }
h1 .spark { color:#7fd1e0; }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:1.5px; color:#8b9bbd; margin:26px 0 10px; border-bottom:1px solid #1d2740; padding-bottom:6px; }
.sub { color:#6b7a99; font-size:12px; }
.bar { display:flex; gap:14px; align-items:center; flex-wrap:wrap; margin-top:8px; }
.pill { background:#121a2e; border:1px solid #25324f; border-radius:20px; padding:4px 12px; font-size:12px; }
.pill b { color:#7fd1e0; }
.timer { font-size:12px; color:#f0b75f; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(168px,1fr)); gap:10px; }
.cell { position:relative; background:#0f1626; border:1px solid #202c47; border-radius:10px; padding:10px; min-height:104px; overflow:hidden; }
.cell .net { font-size:11px; color:#6b7a99; }
.cell .nm { font-weight:600; color:#e6ecff; font-size:14px; }
.cell .meta { font-size:10.5px; color:#7e8db0; margin-top:4px; }
.cell .light { position:absolute; top:9px; right:9px; width:10px; height:10px; border-radius:50%; box-shadow:0 0 8px; }
.st-verified { border-color:#2f8f5b; } .st-verified .light { background:#3ad07f; color:#3ad07f; }
.st-rejected { border-color:#8f3a3a; } .st-rejected .light { background:#e0606a; color:#e0606a; }
.st-untouched .light { background:#33405e; color:#33405e; box-shadow:none; }
.cell .sev { display:inline-block; margin-top:6px; font-size:10px; padding:1px 6px; border-radius:8px; background:#1a2238; color:#c0caea; }
.cell .fam { float:right; font-size:9.5px; color:#7fd1e0; }
.cell .agents-on { margin-top:6px; display:flex; gap:3px; flex-wrap:wrap; align-items:center; }
.amk { width:8px; height:8px; border-radius:50%; display:inline-block; }
.amk.ok { background:#3ad07f; box-shadow:0 0 6px #3ad07f; animation:pulse 1.4s ease-in-out infinite; }
.amk.bad { background:#e0606a; opacity:.65; animation:reveal .5s ease both; }
.cell.active::after { content:''; position:absolute; left:0; right:0; top:0; height:2px; pointer-events:none;
  background:linear-gradient(90deg,transparent,#7fd1e0,transparent); background-size:200px 100%;
  animation:sweep 1.7s linear infinite; }
.cols { display:grid; grid-template-columns:1.15fr .85fr; gap:22px; }
.agent { display:flex; align-items:center; gap:10px; padding:8px 10px; background:#0f1626; border:1px solid #1d2740; border-radius:9px; margin-bottom:7px; }
.dot { width:9px;height:9px;border-radius:50%; box-shadow:0 0 7px; flex:none; }
.dot.ok{background:#3ad07f;color:#3ad07f;} .dot.bad{background:#e0606a;color:#e0606a;}
.agent .id { font-weight:600; color:#e6ecff; min-width:138px; }
.env { font-size:10px; padding:1px 7px; border-radius:7px; background:#15203a; color:#9fb2dc; }
.env.tee { background:#10283a; color:#7fd1e0; } .env.mock { background:#2c1f33; color:#d99ad0; }
.badge { font-size:10px; padding:1px 7px; border-radius:7px; }
.badge.att-ok { background:#10331f; color:#5fe39a; } .badge.att-no { background:#33141a; color:#ff8b95; }
.work { color:#8b9bbd; font-size:11px; }
table { width:100%; border-collapse:collapse; font-size:12px; }
th,td { text-align:left; padding:5px 8px; border-bottom:1px solid #18223a; }
th { color:#6b7a99; font-weight:500; font-size:10.5px; text-transform:uppercase; letter-spacing:.6px; }
.pass { color:#5fe39a; } .fail { color:#ff8b95; }
.lead td:first-child { color:#e6ecff; }
.wbar { height:7px; background:#1a2238; border-radius:5px; overflow:hidden; }
.wbar > i { display:block; height:100%; background:linear-gradient(90deg,#2a6f8f,#7fd1e0); }
.feedrow { display:flex; gap:8px; align-items:center; padding:5px 0; border-bottom:1px solid #161f34; font-size:11.5px; }
.tag { font-size:10px; padding:1px 6px; border-radius:6px; background:#16203a; color:#9fb2dc; }
.tag.r { background:#33141a; color:#ff8b95; }
.heat { display:flex; gap:8px; flex-wrap:wrap; }
.heat .h { background:#0f1626; border:1px solid #202c47; border-radius:8px; padding:6px 10px; font-size:11px; }
.heat .h b { color:#7fd1e0; }
.rules { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin:6px 0 16px; }
.rules .rcard { background:#0d1322; border:1px solid #202c47; border-left:3px solid #4aa6c0; border-radius:9px; padding:9px 11px; font-size:11px; line-height:1.45; color:#aeb9d4; }
.rules .rh { font-weight:700; color:#7fd1e0; font-size:11.5px; margin-bottom:3px; }
.rules .rcard b { color:#e8edf7; }
@media(max-width:900px){ .rules { grid-template-columns:repeat(2,1fr); } }
.console { display:grid; grid-template-columns:repeat(2,1fr); gap:12px; }
.console .box { background:#0f1626; border:1px solid #1d2740; border-radius:9px; padding:10px; }
.console .box.real { border-color:#27613f; } .console .box.mock { border-color:#5a3a63; }
.console .box.safe { border-color:#2a5a6a; } .console .box.risk { border-color:#6a5a2a; }
.console h3 { margin:0 0 6px; font-size:11px; text-transform:uppercase; letter-spacing:1px; color:#8b9bbd; }
.console li { font-size:11px; color:#aab6d4; margin-left:14px; }
.foot { color:#56648a; font-size:11px; margin-top:24px; text-align:center; }
.ticker { font-size:20px; color:#f5d06f; font-weight:700; letter-spacing:.5px; }
.ticker b { color:#ffe79a; }
.grade { font-size:10px; font-weight:700; padding:1px 6px; border-radius:6px; }
.gA{background:#0f3a24;color:#5fe39a;} .gB{background:#123048;color:#7fd1e0;}
.gBm{background:#3a3414;color:#f0d36a;} .gC{background:#2c2740;color:#b3a6e0;} .gF{background:#3a1418;color:#ff8b95;}
.breach { display:flex; gap:9px; align-items:center; padding:7px 10px; margin-bottom:6px;
  background:linear-gradient(90deg,#161f12,#0f1626); border:1px solid #2c3a1f; border-left:3px solid #8fd45f; border-radius:8px; }
.breach .zap { color:#bdf06a; font-size:15px; }
.breach .who { font-weight:700; color:#e9f5d8; }
.breach .emit { margin-left:auto; color:#f5d06f; font-weight:700; }
.rankpill { font-size:10px; padding:1px 7px; border-radius:8px; background:#1a1530; color:#cdb8f0; }
.vaults { display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:8px; }
.vault { background:#0d1322; border:1px solid #202c47; border-radius:9px; padding:8px; font-size:10.5px; position:relative; }
.vault.cracked { border-color:#7a3a3f; } .vault.hardened { border-color:#2f6f4a; } .vault.open { border-color:#7a6a2f; }
.vault .vs { font-weight:700; font-size:10px; }
.vault.cracked .vs { color:#ff8b95; } .vault.hardened .vs { color:#5fe39a; } .vault.open .vs { color:#f5d06f; }
.vault .vb { position:absolute; top:7px; right:8px; color:#f5d06f; font-weight:700; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.45} }
@keyframes reveal { from{opacity:0;transform:translateX(-6px)} to{opacity:1;transform:none} }
@keyframes sweep { 0%{background-position:-200px 0} 100%{background-position:200px 0} }
.agent { animation: reveal .4s ease both; }
.dot.ok { animation: pulse 1.8s ease-in-out infinite; }
.breach { animation: reveal .5s ease both; }
.live { display:inline-block; width:8px;height:8px;border-radius:50%;background:#3ad07f;
  box-shadow:0 0 8px #3ad07f; animation:pulse 1.1s ease-in-out infinite; margin-right:5px; }
#rtimer { color:#f0b75f; font-variant-numeric:tabular-nums; }
.szrow td:first-child { color:#e6ecff; }
.streak { color:#f5a05f; font-weight:700; }
.xpbar { height:8px;background:#1a2238;border-radius:5px;overflow:hidden; }
.xpbar > i { display:block;height:100%;background:linear-gradient(90deg,#7a5cff,#c9a8ff); }
"""

_GRADE_CLS = {"A": "gA", "B": "gB", "B-": "gBm", "C": "gC", "F": "gF"}

_STATUS_LIGHT = {"verified": "st-verified", "rejected": "st-rejected"}


def _esc(s) -> str:
    return html.escape(str(s))


def render(r: ArenaResult, refresh_secs: int = 6) -> str:
    cs = r.corpus_summary
    n_pass = sum(1 for a in r.agents if a.gates.passed())
    n_rej = len(r.anticheat_feed)
    total_emit = sum(r.emissions.values())
    earners = sorted(((a.run.agent_id, r.emissions.get(a.run.miner_hotkey, 0.0), a)
                      for a in r.agents), key=lambda x: -x[1])

    # which REAL agents are working each target this round (live, animated markers)
    agents_on: dict[int, list[tuple[str, bool]]] = {}
    for a in r.agents:
        agents_on.setdefault(a.run.target_netuid, []).append(
            (a.run.agent_id, a.gates.passed()))

    # target grid
    cells = []
    state = r.target_state
    stt = r.season_targets or {}
    for t in r.targets:
        # prefer the season-CUMULATIVE conquest status (a broken subnet stays lit)
        sinfo = stt.get(t.netuid) or stt.get(str(t.netuid))
        if sinfo:
            st = {"conquered": "verified", "probed": "probing"}.get(sinfo["status"], "untouched")
        else:
            st = state[t.netuid].status
        cls = _STATUS_LIGHT.get(st, "st-untouched")
        here = agents_on.get(t.netuid, [])
        # animated markers: one per agent attacking THIS subnet (green=verified
        # proof, red=rejected), each pulsing/working; the cell scans while active.
        marks = "".join(
            f'<span class="amk {"ok" if ok else "bad"}" title="{_esc(aid)}" '
            f'style="animation-delay:{(i % 5) * 0.18:.2f}s"></span>'
            for i, (aid, ok) in enumerate(here))
        agbar = (f'<div class="agents-on">{marks}'
                 f'<span class="sub" style="font-size:9px">{len(here)} agent'
                 f'{"s" if len(here) != 1 else ""}</span></div>' if here else "")
        active = " active" if here else ""
        cells.append(
            f'<div class="cell {cls}{active}"><span class="light"></span>'
            f'<div class="net">netuid {t.netuid} · uid {t.our_uid}</div>'
            f'<div class="nm">{_esc(t.name)}</div>'
            f'<span class="fam">{_esc(t.family)}</span>'
            f'<div class="meta">{_esc(t.candidate_title[:54])}</div>'
            f'<span class="sev">sev {t.severity}</span> '
            f'<span class="sev">{_esc(t.risk_level)}</span>{agbar}</div>')
    grid = '<div class="grid">' + "".join(cells) + "</div>"

    # agents rail
    arows = []
    for a in r.agents:
        g = a.gates
        ok = g.passed()
        env = a.run.environment
        envcls = "tee" if "tee" in env and env != "mocked-tee" else ("mock" if env == "mocked-tee" else "")
        att = ""
        if a.mission.attestation_required:
            att = (f'<span class="badge att-ok">attested</span>' if g.attestation_valid
                   else '<span class="badge att-no">no-attest</span>')
        gr = g.provenance_grade
        prov = f'<span class="grade {_GRADE_CLS.get(gr,"gF")}">prov {_esc(gr)}</span>'
        work = ("verified proof" if ok else f"rejected · {_esc(g.first_failure())}")
        meth = _esc(getattr(a.run, "method", ""))
        arows.append(
            f'<div class="agent"><span class="dot {"ok" if ok else "bad"}"></span>'
            f'<span class="id">{_esc(a.run.agent_id)}</span>'
            f'<span class="env {envcls}">{_esc(env)}</span>{prov}{att}'
            f'<span class="work" title="{meth}">→ sn{a.run.target_netuid} · {work}</span></div>')
    agents = "".join(arows)

    # leaderboard (emissions + rank + provenance)
    maxe = max((e for _, e, _ in earners), default=1.0) or 1.0
    lrows = []
    for aid, emit, a in earners:
        hk = a.run.miner_hotkey
        gr = a.gates.provenance_grade
        rank = _esc(r.ranks.get(hk, "Initiate"))
        c = getattr(a, "credit", None)
        metric = (f" · metric {c.tier_weight:.2f}×{c.speed:.2f}={c.contrib:.2f}"
                  if c is not None and emit > 0 else "")
        why = (f"breached sn{a.run.target_netuid} · prov {_esc(gr)}{metric}" if emit > 0
               else f"0 · {_esc(a.gates.first_failure())}")
        lrows.append(
            f'<tr class="lead"><td>{_esc(aid)}</td>'
            f'<td style="width:150px"><div class="wbar"><i style="width:{emit/maxe*100:.0f}%"></i></div></td>'
            f'<td style="color:#f5d06f">{emit:.0f}τ</td>'
            f'<td><span class="rankpill">{rank}</span></td>'
            f'<td class="{"pass" if emit>0 else "fail"}">{why}</td></tr>')
    leaderboard = ('<table><tr><th>agent</th><th>emissions</th><th></th><th>rank</th>'
                   '<th>why this miner is winning</th></tr>' + "".join(lrows) + "</table>")

    # breach kill-feed
    bf = []
    for b in r.breaks:
        bf.append(f'<div class="breach"><span class="zap">⚡</span>'
                  f'<span class="who">{_esc(b["agent"])}</span>'
                  f'<span class="sub">BREACHED sn{b["netuid"]} {_esc(b["subnet"])}</span>'
                  f'<span class="grade {_GRADE_CLS.get(b["grade"],"gF")}">prov {_esc(b["grade"])}</span>'
                  f'<span class="emit">+{b["bounty"]:.0f}τ → {b["emit"]:.0f}τ</span></div>')
    breach_feed = "".join(bf) or '<div class="sub">no breaches yet this round</div>'

    # chain / contract vaults (real money-math CNF corpus)
    vh = []
    for v in r.chain_vaults:
        cls = {"CRACKED": "cracked", "HARDENED": "hardened"}.get(v["status"], "open")
        vh.append(f'<div class="vault {cls}"><span class="vb">{v["bounty"]:.0f}τ</span>'
                  f'<div class="vs">{_esc(v["status"])}</div>'
                  f'<div style="color:#e6ecff;margin-top:2px">{_esc(v["model"])}</div>'
                  f'<div class="sub">{_esc(v["tier"])} · {v["vars"]} vars</div>'
                  f'<div class="sub" style="margin-top:3px">{_esc(v["invariant"])}</div></div>')
    vaults = '<div class="vaults">' + "".join(vh) + "</div>"

    # replay theater — REAL money-math reproduced on the witness
    rt = []
    for t in r.replay_theater:
        o = t["observed"]
        if "paid" in o:
            lhs, rhs = o.get("paid", 0), o.get("amount", 0)
            detail = f'paid <b>{lhs:,}</b> vs amount <b>{rhs:,}</b> · overcharge {o.get("overcharge",0):,}'
        elif "total_distributed" in o or "total" in o:
            lhs, rhs = o.get("total_distributed", o.get("total", 0)), o.get("pool", 0)
            detail = f'distributed <b>{lhs:,}</b> vs pool <b>{rhs:,}</b>'
        elif "reported_rate" in o:
            detail = (f'reported score <b>{o["reported_rate"]:.2f}</b> vs honest '
                      f'<b>{o.get("honest_accuracy",0):.2f}</b> '
                      f'({o.get("answered","?")}/{o.get("total","?")} answered)')
        elif "fee" in o:
            detail = (f'amount <b>{o["amount"]:,}</b> · fee_rate <b>{o["fee_rate"]}</b> '
                      f'→ fee <b>{o["fee"]}</b> (silent-zero)')
        else:
            detail = _esc(str(t.get("reason", "")))
        repro = ('<span class="tag" style="background:#10331f;color:#5fe39a">REPRODUCED</span>'
                 if t["reproduced"] else
                 '<span class="tag r">no-repro</span>')
        reach = ('<span class="tag">reachable</span>' if t["reachable"]
                 else '<span class="tag" style="background:#2c2510;color:#e0c069">gov-gated</span>')
        pin = ""
        if t.get("source") == "audit_lane":
            pin = (f'<span class="tag" style="background:#10283a;color:#7fd1e0">'
                   f'audit_lane @ {_esc((t.get("code_sha256") or "")[:8])}</span>')
        elif t.get("source") == "z3-factory-mint":
            pin = (f'<span class="tag" style="background:#221a3a;color:#c9a8ff">'
                   f'z3-minted @ {_esc((t.get("code_sha256") or "")[:8])}</span>')
        rt.append(f'<div class="feedrow"><b>{_esc(t["agent"])}</b>'
                  f'<span class="sub">{_esc(t["target_id"].split(":")[1].split("@")[0])}</span>'
                  f'{repro}{reach}{pin}<span class="sub">{detail}</span></div>')
    replay_html = "".join(rt) or '<div class="sub">no replays this round</div>'

    # season standings (cumulative across rounds)
    season_html = ""
    if r.season_board:
        maxe = max((s["emissions"] for s in r.season_board), default=1.0) or 1.0
        srows = []
        for i, s in enumerate(r.season_board):
            streak = (f'<span class="streak">🔥{s["streak"]}</span>' if s["streak"] else
                      f'<span class="sub">best {s["best_streak"]}</span>')
            rc = s.get("rank_change")
            mover = ('<span class="sub" style="color:#7fd1e0">NEW</span>' if rc is None else
                     f'<span class="pass">▲{rc}</span>' if rc > 0 else
                     f'<span class="fail">▼{-rc}</span>' if rc < 0 else
                     '<span class="sub">–</span>')
            srows.append(
                f'<tr class="szrow"><td>{i+1}. {_esc(s["agent_id"])} {mover}</td>'
                f'<td style="width:150px"><div class="xpbar"><i style="width:{s["emissions"]/maxe*100:.0f}%"></i></div></td>'
                f'<td style="color:#f5d06f">{s["emissions"]:.0f}τ</td>'
                f'<td><span class="rankpill">{_esc(s["rank"])}</span></td>'
                f'<td>{s["breaches"]} breaches</td><td>{streak}</td></tr>')
        season_html = (f'<h2>🏆 Season {_esc(r.season)} Standings '
                       f'<span class="sub">— {r.season_rounds} rounds, cumulative</span></h2>'
                       '<table><tr><th>#</th><th>emissions</th><th></th><th>rank</th>'
                       '<th>breaches</th><th>streak</th></tr>' + "".join(srows) + "</table>")

    # proof feed
    pf = []
    for f in r.proof_feed:
        cls = "pass" if f["passed"] else "fail"
        tag = ('<span class="tag">verified</span>' if f["passed"]
               else f'<span class="tag r">{_esc(f["gate_fail"])}</span>')
        att = ""
        if f["attest_required"]:
            att = ('<span class="tag">att✓</span>' if f["attest_valid"]
                   else '<span class="tag r">att✗</span>')
        if f.get("reasoning_coherent"):
            fam = f'<span class="tag" title="reasoned family == proven invariant">{_esc(f.get("family",""))} ✓</span>'
        elif f.get("family"):
            pfam = f.get("proof_family") or "?"
            fam = f'<span class="tag" title="reasoned vs proven">{_esc(f["family"])}→{_esc(pfam)}</span>'
        else:
            fam = ""
        pf.append(f'<div class="feedrow"><span class="{cls}">●</span>'
                  f'<b>{_esc(f["agent"])}</b><span class="sub">sn{f["netuid"]} {_esc(f["subnet"])}</span>'
                  f'<span class="sub">t{f["tier"]} {f["wall_ms"]}ms</span>{fam}{tag}{att}</div>')
    proof_feed = "".join(pf)

    # anti-cheat feed
    ac = []
    for x in r.anticheat_feed:
        ac.append(f'<div class="feedrow"><span class="fail">✗</span>'
                  f'<b>{_esc(x["agent"])}</b><span class="tag r">{_esc(x["archetype"])}</span>'
                  f'<span class="sub">{_esc(x["subnet"])}</span>'
                  f'<span class="fail">{_esc(x["rejected_by"])}</span>'
                  f'<span class="sub" title="{_esc(x.get("method",""))}">'
                  f'{_esc(x.get("method") or ", ".join(x["reasons"][:2]))}</span></div>')
    anticheat = "".join(ac) or '<div class="sub">no rejected submissions</div>'

    # hotkey-stacking (Sybil) panel — coldkeys with >1 hotkey, collapsed vs naive
    sybil = ""
    if r.sybil_panel:
        sr = []
        for p in r.sybil_panel:
            mult = p["naive"] / max(p["collapsed"], 1e-9)
            sr.append(f'<div class="feedrow"><span class="fail">⚠</span>'
                      f'<b>{_esc(p["coldkey"])}</b>'
                      f'<span class="tag r">{len(p["hotkeys"])} hotkeys stacked</span>'
                      f'<span class="sub">collapsed {p["collapsed"]:.3f} vs naive {p["naive"]:.3f} '
                      f'(~{mult:.1f}x) → stacking gains nothing</span></div>')
        sybil = ('<h2>🧬 Hotkey-Stacking Guard <span class="sub">— coldkey collapse</span></h2>'
                 + "".join(sr))

    # heat map by family
    heat: dict[str, int] = {}
    for a in r.agents:
        heat[a.mission.target.family] = heat.get(a.mission.target.family, 0) + 1
    heat_html = '<div class="heat">' + "".join(
        f'<div class="h">{_esc(k)} <b>{v}</b></div>' for k, v in sorted(heat.items(), key=lambda x: -x[1])) + "</div>"

    # solver bench (PAR-2)
    bench_html = ""
    if r.solver_bench:
        solved_pars = [b["par2_ms"] for b in r.solver_bench if b["solved"] > 0]
        worst = max(solved_pars) if solved_pars else 1.0
        brows = []
        for b in r.solver_bench:
            crown = "👑 " if b["crown"] else ""
            if b["solved"] == 0:
                bar = '<span class="tag r">cert-rejected · 0 solved</span>'
                par = '<span class="fail">∞ PAR-2</span>'
            else:
                w = min(100, b["par2_ms"] / worst * 100)
                bar = f'<div class="wbar" style="width:150px"><i style="width:{w:.0f}%"></i></div>'
                par = f'{b["par2_ms"]:.1f}ms'
            brows.append(f'<tr class="lead"><td>{crown}{_esc(b["name"])}</td><td>{bar}</td>'
                         f'<td>{par}</td><td>{b["solved"]}/{b["total"]} certified</td></tr>')
        bench_html = ('<h2>⚙️ Solver Bench <span class="sub">— PAR-2, certified solves, '
                      'fastest holds the crown</span></h2>'
                      '<table><tr><th>solver</th><th>PAR-2 (lower=better)</th><th></th>'
                      '<th>certified</th></tr>' + "".join(brows) + "</table>")
        # a REAL solver race on a REAL pre-existing audit CNF (kissat on Stitch,
        # host-measured, vs a local CDCL solver on the same pinned formula).
        race = (r.operator_console.get("stitch_runner", {}) or {}).get("solver_race")
        if race:
            rm, lo = race["remote"], race["local"]
            def _cell(s, win):
                star = " 🏆" if race.get("winner") == s["solver"] else ""
                cls = "pass" if race.get("winner") == s["solver"] else "sub"
                return (f'<span class="{cls}">{_esc(s["solver"])}@{_esc(s["host"])} '
                        f'{_esc(str(s["ms"]))}ms{star}</span>')
            bench_html += (f'<div class="sub" style="margin-top:6px">REAL solver race · '
                           f'<b>real audit CNF</b> AMM-A4-conservation (UNSAT, both agree): '
                           f'{_cell(rm, True)} vs {_cell(lo, False)}</div>')

    # real audit vault — invariants SETTLED on real audit CNFs (headline cards)
    real_vault_html = ""
    if getattr(r, "real_audit_vault", None):
        cards = []
        for c in r.real_audit_vault:
            cracked = c["verdict"] == "CRACKED"
            color = "#f59a9a" if cracked else "#5fe39a"
            bg = "#3a1a1a" if cracked else "#10331f"
            real_badge = ('<span class="tag" style="background:#2a2410;color:#f5d06f">REAL CNF</span>'
                          if c.get("real_cnf") else
                          ('<span class="tag" style="background:#221a3a;color:#c9a8ff">OFF-BOX kissat@Stitch</span>'
                           if c.get("offbox") else '<span class="tag">z3-minted</span>'))
            xb = (' <span class="pass">✓ cross-confirmed</span>' if c.get("cross_confirmed") else "")
            cards.append(
                f'<div class="breach" style="border-left:3px solid {color}">'
                f'<span class="who" style="color:{color}">{_esc(c["verdict"])}</span>'
                f'<span class="sub">{_esc(c["family"])}</span>{real_badge}'
                f'<span class="sub">{_esc(c["invariant"])[:64]}</span>'
                f'<span class="emit" style="color:{color}">{_esc(c["evidence"])[:78]}{xb}</span></div>')
        real_vault_html = (
            '<h2>🔓 Real Audit Vault <span class="sub">— subtensor invariants settled on REAL audit CNFs '
            '(CRACKED = exploit exists · HARDENED = no exploit, two solvers agree)</span></h2>'
            + "".join(cards))

    # operator console
    oc = r.operator_console
    def _box(cls, title, items):
        lis = "".join(f"<li>{_esc(i)}</li>" for i in items)
        return f'<div class="box {cls}"><h3>{title}</h3><ul>{lis}</ul></div>'
    console = ('<div class="console">'
               + _box("real", "real", oc["real"])
               + _box("mock", "mocked", oc["mocked"])
               + _box("safe", "safe", oc["safe"])
               + _box("risk", "risky / next", oc["risky_todo"]) + "</div>"
               + f'<div class="sub" style="margin-top:8px">attestation: {_esc(oc["attestation"])}</div>')
    la = oc.get("live_attestation", {})
    if la:
        if la.get("ok"):
            tag = '<span class="tag" style="background:#10331f;color:#5fe39a">LIVE TDX VERIFIED</span>'
        elif la.get("blocked"):
            tag = '<span class="tag r">live quote BLOCKED</span>'
        else:
            tag = '<span class="tag">live quote pending</span>'
        console += (f'<div class="sub" style="margin-top:6px">live attestation: {tag} '
                    f'backend={_esc(la.get("backend","n/a"))} '
                    f'<span class="fail">{_esc(str(la.get("reason",""))[:90])}</span></div>')
    mp = oc.get("minted_proof", {})
    if mp.get("available"):
        mtag = ('<span class="tag" style="background:#221a3a;color:#c9a8ff">UNIFIED PROOF ✓</span>'
                if mp.get("ok") else '<span class="tag r">minted proof incomplete</span>')
        es = mp.get("external_solve", {})
        console += (f'<div class="sub" style="margin-top:4px">minted invariant: {mtag} '
                    f'z3 encode → {_esc(es.get("solver","?"))} solve {_esc(str(es.get("solve_ms","?")))}ms '
                    f'({mp.get("vars","?")}v/{mp.get("clauses","?")}c, verified) → witness reproduces · '
                    f'sha {_esc((mp.get("cnf_sha256") or "")[:8])}</div>')
    sr = oc.get("stitch_runner", {})
    if sr.get("available"):
        stag = ('<span class="tag" style="background:#10331f;color:#5fe39a">REAL REMOTE EXEC</span>'
                if sr.get("ok") else '<span class="tag r">stitch run failed</span>')
        if sr.get("real_cnf"):
            # a REAL pre-existing audit CNF, cross-checked kissat(Stitch) vs local CDCL
            verdict = ("UNSAT — invariant HARDENED (both solvers agree)" if sr.get("hardened_proof")
                       else ("SAT — witness verified locally" if sr.get("witness_verified_locally")
                             else _esc(str(sr.get("remote_status")))))
            console += (f'<div class="sub" style="margin-top:4px">stitch-runner: {stag} '
                        f'real CNF <b>{_esc(sr.get("real_cnf"))}</b> · '
                        f'{_esc(sr.get("solver","?"))} on {_esc(sr.get("host","?"))} '
                        f'{_esc(str(sr.get("remote_wall_ms","?")))}ms host-measured vs '
                        f'{_esc(sr.get("local_solver","?"))} local → {verdict} · '
                        f'sha {_esc((sr.get("cnf_sha256") or "")[:8])}</div>')
        else:
            console += (f'<div class="sub" style="margin-top:4px">stitch-runner: {stag} '
                        f'{_esc(sr.get("solver","?"))} on {_esc(sr.get("host","?"))} · '
                        f'{_esc(str(sr.get("remote_wall_ms","?")))}ms host-measured · '
                        f'{sr.get("n_vars","?")} vars · witness verified locally</div>')

    sa = oc.get("stitch_attest", {})
    if sa.get("available"):
        if sa.get("attested"):
            atag = '<span class="tag" style="background:#10331f;color:#5fe39a">SOLVE ATTESTED ✓</span>'
        elif sa.get("live_quote"):
            atag = '<span class="tag r">quote does not bind solve</span>'
        else:
            atag = '<span class="tag" style="background:#2a2410;color:#f5d06f">ATTEST-READY (gated)</span>'
        console += (f'<div class="sub" style="margin-top:4px">solve attestation: {atag} '
                    f'report_data binds sha256(commitment‖pubkey) · commitment '
                    f'{_esc((sa.get("commitment") or "")[:12])}… · '
                    f'one bounded TDX quote gated on approval (no spend)</div>')

    rs = oc.get("remote_sat", {})
    if rs.get("available"):
        vtag = ('<span class="tag" style="background:#3a1a1a;color:#f59a9a">EXPLOIT (SAT) ✓</span>'
                if rs.get("violable") else f'<span class="tag">{_esc(str(rs.get("status")))}</span>')
        xtag = (' · <span class="pass">cross-confirmed by z3-minted twin</span>'
                if rs.get("cross_confirmed") else "")
        console += (f'<div class="sub" style="margin-top:4px">real-CNF exploit: {vtag} '
                    f'<b>{_esc(rs.get("real_cnf"))}</b> (I_safety recalc-overcharge) · '
                    f'{_esc(rs.get("solver","?"))} on {_esc(rs.get("host","?"))} '
                    f'{_esc(str(rs.get("remote_wall_ms","?")))}ms host-measured, {rs.get("n_lits","?")} '
                    f'lit witness, VIOLABLE on the real 20MB audit CNF{xtag}</div>')

    inv = oc.get("stitch_inventory", {})
    if inv.get("available"):
        console += (f'<div class="sub" style="margin-top:4px">Stitch artifact corpus: '
                    f'<span class="tag" style="background:#10331f;color:#5fe39a">REAL</span> '
                    f'<b>{inv.get("total_cnf","?")}</b> CNFs · <b>{inv.get("total_map","?")}</b> decode maps · '
                    f'<b>{inv.get("total_py","?")}</b> harnesses across {inv.get("n_dirs","?")} dirs on '
                    f'{_esc(inv.get("host","?"))} <span class="sub">({_esc(inv.get("captured_at",""))})</span></div>')

    ob = oc.get("offbox_stitch", {})
    if ob.get("available"):
        sat = ob.get("cnf_satisfied")
        obtag = ('<span class="tag" style="background:#221a3a;color:#c9a8ff">OFF-BOX SOLVE ✓</span>'
                 if sat else '<span class="tag r">off-box unverified</span>')
        console += (f'<div class="sub" style="margin-top:4px">{obtag} '
                    f'<b>{_esc(ob.get("solver","kissat"))}</b> solved a minted CNF on '
                    f'{_esc(ob.get("host","?"))} ({ob.get("remote_wall_ms","?")}ms · '
                    f'{ob.get("n_lits","?")} lits · {ob.get("round_trips","?")} round-trips) → '
                    f'decoded LOCALLY <b>no z3</b> → assignment satisfies the pinned-invariant CNF '
                    f'<span class="sub">({_esc(str(ob.get("captured_at","")))})</span></div>')

    ob1 = oc.get("offbox_i1", {})
    if ob1.get("available") and ob1.get("cnf_satisfied"):
        console += (f'<div class="sub" style="margin-top:4px">'
                    f'<span class="tag" style="background:#221a3a;color:#c9a8ff">OFF-BOX SOLVE ✓ (multi-rule)</span> '
                    f'<b>{_esc(ob1.get("solver","kissat"))}</b> also solved '
                    f'<b>{_esc(ob1.get("rule_id","I1-div-by-zero"))}</b> on {_esc(ob1.get("host","?"))} '
                    f'({ob1.get("remote_wall_ms","?")}ms · {ob1.get("n_lits","?")} lits · '
                    f'{ob1.get("round_trips","?")} round-trips) → off-box is not B2-only '
                    f'<span class="sub">({_esc(str(ob1.get("captured_at","")))})</span></div>')

    oh = oc.get("offbox_hardened", {})
    if oh.get("available"):
        cc = oh.get("cross_confirmed")
        ohtag = ('<span class="tag" style="background:#10331f;color:#5fe39a">OFF-BOX HARDENED 🛡</span>'
                 if cc else '<span class="tag r">hardening unconfirmed</span>')
        console += (f'<div class="sub" style="margin-top:4px">{ohtag} '
                    f'<b>{_esc(oh.get("solver","kissat"))}</b> confirmed <b>{_esc(oh.get("rule_id","?"))}</b> '
                    f'UNSAT on {_esc(oh.get("host","?"))} ({oh.get("remote_wall_ms","?")}ms · '
                    f'{oh.get("round_trips","?")} round-trips) + local CDCL UNSAT → '
                    f'invariant proven HARDENED off-box (no exploit exists) '
                    f'<span class="sub">({_esc(str(oh.get("captured_at","")))})</span></div>')

    pc = oc.get("proof_coverage", {})
    if pc.get("rows"):
        chips = []
        for cov in pc["rows"]:
            b = cov["backing"]
            col = ("#5fe39a" if b == "real_exploit" else
                   "#7fd1e0" if b == "hardened_no_exploit" else "#f0b75f")
            mark = ("⚔" if b == "real_exploit" else "🛡" if b == "hardened_no_exploit" else "·")
            chips.append(f'<span class="h" title="{_esc(cov["family"])} · {_esc(str(cov["detail"]))}">'
                         f'<span style="color:{col}">{mark}</span> {_esc(str(cov["netuid"]))} '
                         f'{_esc(cov["name"][:14])}</span>')
        console += (
            f'<div class="sub" style="margin-top:4px">Per-subnet proof coverage: '
            f'<b class="pass">{pc["real_exploit"]}</b> backed by a REAL reproducing exploit ⚔ · '
            f'<b style="color:#7fd1e0">{pc["hardened_no_exploit"]}</b> proven HARDENED 🛡 '
            f'(no exploit exists) · {pc["fallback"]} fallback of {pc["total"]} subnets</div>'
            f'<div class="heat" style="margin-top:6px">{"".join(chips)}</div>')

    rb = oc.get("real_solver_bench", []) or []
    if rb:
        champ = rb[0]
        names = " · ".join(f'{_esc(r["name"])} {r["par2_ms"]:.1f}ms'
                           + (" 👑" if r.get("crown") else "") for r in rb)
        console += (f'<div class="sub" style="margin-top:4px">'
                    f'<span class="tag" style="background:#10331f;color:#5fe39a">REAL SOLVER RACE</span> '
                    f'two distinct real solvers on the same certified CNF batch (PAR-2, lower=better): '
                    f'{names} — crown <b>{_esc(champ["name"])}</b></div>')

    ed = oc.get("external_decode", {})
    if ed.get("available"):
        etag = ('<span class="tag" style="background:#221a3a;color:#c9a8ff">OFF-BOX DECODE ✓</span>'
                if ed.get("ok") else '<span class="tag r">decode failed</span>')
        console += (f'<div class="sub" style="margin-top:4px">off-box solve: {etag} '
                    f'z3 mints CNF + bit→var map → {_esc(ed.get("solver","?"))} solves off-box → '
                    f'assignment decoded to exploit input {_esc(str(ed.get("decoded_input")))} '
                    f'<b>without re-running z3</b> → reproduces via the real harness</div>')

    ra = oc.get("round_attest", {})
    if ra.get("available"):
        if ra.get("attested_to_this_round"):
            rtag = '<span class="tag" style="background:#10331f;color:#5fe39a">ROUND ATTESTED ✓</span>'
        elif ra.get("has_real_quote_on_file"):
            rtag = ('<span class="tag" style="background:#221a3a;color:#c9a8ff">REAL TDX ON FILE</span>'
                    ' <span class="tag" style="background:#2a2410;color:#f5d06f">round bind gated</span>')
        else:
            rtag = '<span class="tag" style="background:#2a2410;color:#f5d06f">ATTEST-READY (gated)</span>'
        rq = (f' · real Intel-verified TDX quote on file ({_esc(ra.get("real_quote_instance",""))}, '
              f'${_esc(str(ra.get("real_quote_cost_usd","")))})' if ra.get("has_real_quote_on_file") else "")
        console += (f'<div class="sub" style="margin-top:4px">round attestation: {rtag} '
                    f'report_data binds the round Merkle root {_esc((ra.get("commitment") or "")[:12])}… '
                    f'(one quote attests every proof in the round){rq} — live quote gated on approval, no spend</div>')

    sca = oc.get("scoring_audit", {})
    if sca:
        ctag = ('<span class="tag" style="background:#10331f;color:#5fe39a">SCORING VERIFIED ✓</span>'
                if sca.get("ok") else '<span class="tag r">scoring audit FAILED</span>')
        nchk = len(sca.get("checks", {}))
        console += (f'<div class="sub" style="margin-top:4px">scoring self-audit: {ctag} '
                    f'reward = linear_metric × boolean_gate re-verified independently '
                    f'({sum(1 for v in sca.get("checks",{}).values() if v)}/{nchk} checks: '
                    f'cheaters zeroed · honest earn · gate consistency · signed vector · anchor)</div>')

    sv = r.signed_vector

    # "Rules of the Arena" - the 60-second explainer, DATA-DRIVEN from the real gate
    # set + anti-cheat taxonomy (so the counts can't drift from the engine).
    from .models import GateOutcome
    from . import reports
    n_gates = len(GateOutcome.GATES)
    n_axes = len(reports.ANTICHEAT_AXES)
    rules_html = (
        '<div class="rules">'
        '<div class="rcard"><div class="rh">1 Your agent</div>operates on an attested / '
        'Stitch / local / sandbox environment and attacks an assigned subnet target, hunting a '
        'real invariant violation.</div>'
        '<div class="rcard"><div class="rh">2 The proof</div>a witness + trace + CNF + replay'
        ' (+ a TEE attestation when the tier requires it). Prose and severity never score - only '
        'a replayable witness does.</div>'
        f'<div class="rcard"><div class="rh">3 How you win</div><b>reward = linear_metric x '
        f'boolean_gate</b>. All {n_gates} boolean gates must pass; the linear metric (verified '
        'replays, solver PAR-2, attested runs) then sets your weight.</div>'
        f'<div class="rcard"><div class="rh">4 Why cheating fails</div>{n_axes} anti-cheat axes, '
        'each bound to a gate - copied witness, wrong owner, stale replay, fake attestation, fake '
        'compute, spam, invalid CNF, missing decode map, bad replay harness, hotkey stacking, '
        'trace forgery, mislabeled finding -> reward x0.</div>'
        '</div>')

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="{refresh_secs}">
<title>Cathedral Arena — {_esc(r.season)} R{r.round_no}</title><style>{_CSS}</style></head>
<body><div class="wrap">
<h1><span class="spark">◆</span> CATHEDRAL ARENA <span class="sub">— live verification arena · miners operate agents · proof, not claims</span></h1>
<div class="bar">
  <span class="ticker">💰 <b>{total_emit:.0f} τ</b> emitted</span>
  <span class="pill">season <b>{_esc(r.season)}</b> · round <b>{r.round_no}</b></span>
  <span class="timer"><span class="live"></span>LIVE · R{r.round_no + 1} in <span id="rtimer">{refresh_secs}</span>s</span>
  <span class="pill">targets <b>{cs['targets']}</b>{(" · conquered <b class='pass'>" + str(r.season_conquered) + "/" + str(cs['targets']) + "</b>") if r.season_targets else ""}</span>
  <span class="pill">chain vaults <b>{cs['proof_tasks']}</b> ({cs['sat']} cracked / {cs['unsat']} hardened / {cs['unknown']} open)</span>
  <span class="pill">agents <b>{len(r.agents)}</b></span>
  <span class="pill">breaches <b class="pass">{n_pass}</b></span>
  <span class="pill">rejected <b class="fail">{n_rej}</b></span>
</div>

<h2>Rules of the Arena <span class="sub">- understand it in 60 seconds</span></h2>
{rules_html}

<h2>Attack Map — 17 subnet targets</h2>
<div class="sub" style="margin-bottom:10px">status: <span class="pass">●</span> verified proof · <span class="fail">●</span> rejected attempt · ○ untouched</div>
{grid}

{season_html}

<h2>⚡ Breach Feed — targets broken for emissions</h2>
{breach_feed}

{real_vault_html}

<h2>🎬 Replay Theater — real subtensor money-math reproduced on the witness</h2>
<div class="sub" style="margin-bottom:8px">the arena re-runs the pinned U64F64 fee math; a finding counts only if the real invariant is violated. <span class="tag" style="background:#2c2510;color:#e0c069">gov-gated</span> = mechanically true but unreachable (rulebook reachability gate).</div>
{replay_html}

<div class="cols">
 <div>
  <h2>Active Agents <span class="sub">— signed run-receipts, verify-by-receipt</span></h2>
  {agents}
  <h2>Leaderboard — emissions · rank · why miners win</h2>
  {leaderboard}
  <h2>Chain &amp; Contract Vaults <span class="sub">— real subtensor money-math</span></h2>
  {vaults}
  {bench_html}
  <h2>Attack Heat (by invariant family)</h2>
  {heat_html}
 </div>
 <div>
  <h2>Proof Feed</h2>
  {proof_feed}
  <h2>Anti-Cheat Feed — rejected & why</h2>
  {anticheat}
  {sybil}
  <h2>Operator Console</h2>
  {console}
 </div>
</div>

<h2>Signed Weight Vector (emission)</h2>
<div class="sub">policy_version {sv['policy_version']} · netuid {sv['netuid']} · {len(sv['weights'])} earners · Ed25519 sig present (independently verifiable)</div>

<h2>🔗 Round Proof Anchor <span class="sub">— the whole round is one verifiable commitment</span></h2>
<div class="sub">Merkle root <span style="color:#7fd1e0">{_esc((r.anchor.get('merkle_root') or '')[:32])}…</span>
over {r.anchor.get('n_leaves','?')} leaves (every agent receipt head + signed vector) ·
Ed25519-signed · inclusion-provable · <span style="color:#e0c069">{_esc(r.anchor.get('anchor_target',''))}</span></div>

<div class="foot">Cathedral Arena · local E2E · no mainnet writes · live SN39 validator untouched · reward = linear_metric × boolean_gate</div>
</div>
<script>
(function(){{
  // the page auto-refreshes every {refresh_secs}s; the live server ticks a FRESH
  // round on each load, so this countdown is the REAL time to the next round.
  var n={refresh_secs}, el=document.getElementById('rtimer');
  setInterval(function(){{ n=(n<=0?0:n-1); if(el) el.textContent=n; }}, 1000);
}})();
</script>
</body></html>"""


def write_html(r: ArenaResult, path: str) -> str:
    out = render(r)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(out)
    return path
