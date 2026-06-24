"""Render a GameResult so the mechanism is obvious at a glance: who earned what,
and WHY each gate fired. Pure formatting — no scoring lives here.
"""
from __future__ import annotations

from .engine import GameResult
from .reward import (MinerResult, SolveCredit, coldkey_totals, naive_weights,
                     verify_vector)


def _why(r: MinerResult, live: tuple[bool, str]) -> str:
    if not live[0]:
        return f"DARK — liveness gate=0 ({live[1]})"
    if not r.credits:
        return "no challenges"
    earned = sum(1 for c in r.credits if c.contrib > 0)
    zeroed = [c for c in r.credits if c.contrib == 0]
    if earned and not zeroed:
        return f"clean: {earned}/{len(r.credits)} solves credited"
    reasons = sorted({c.reason for c in zeroed if c.reason})
    tag = ", ".join(reasons) if reasons else "zeroed"
    if earned:
        return f"{earned}/{len(r.credits)} credited; rest gated: {tag}"
    return f"all gated: {tag}"


def render(result: GameResult) -> str:
    out: list[str] = []
    out.append("=" * 78)
    out.append(f"  CATHEDRAL — local game   |   rounds={result.rounds}   "
               f"|   reward = metric x gate")
    out.append("=" * 78)
    header = f"  {'archetype':14s} {'live':4s} {'metric':>8s} {'reward':>8s} {'weight':>8s}  why"
    out.append(header)
    out.append("  " + "-" * 74)

    rows = sorted(result.results.values(), key=lambda r: (-r.weight, -r.reward, r.hotkey))
    for r in rows:
        live = result.live.get(r.hotkey, (r.live, ""))
        flag = "yes" if live[0] else "NO"
        out.append(f"  {r.archetype:14s} {flag:4s} {r.metric:8.3f} {r.reward:8.3f} "
                   f"{r.weight:8.3f}  {_why(r, live)}")

    out.append("  " + "-" * 74)
    out.append("")
    out.append("  per-solve ledger (verified x attest_gate x tier_weight x speed):")
    for c in sorted(result.credits, key=lambda c: (c.hotkey, c.challenge_id)):
        v = "1" if c.verified else "0"
        a = "1" if c.attest_gate else "0"
        note = f"  <- {c.reason}" if c.reason else ""
        out.append(f"    {c.hotkey:14s} t{c.tier} ver={v} att={a} "
                   f"tw={c.tier_weight:.1f} spd={c.speed:.3f} "
                   f"=> contrib={c.contrib:.3f}{note}")

    # Sybil-resistance panel: for any coldkey holding >1 hotkey, show the
    # collapsed (real) total weight vs the naive (no-collapse) total it would
    # have collected. Collapse caps an operator at one identity's fair share.
    ck_of = {hk: r.coldkey for hk, r in result.results.items()}
    members: dict[str, list[str]] = {}
    for hk, ck in ck_of.items():
        members.setdefault(ck, []).append(hk)
    multi = {ck: hks for ck, hks in members.items() if len(hks) > 1}
    if multi:
        collapsed = {hk: r.weight for hk, r in result.results.items()}
        naive = naive_weights(result.results)
        ct_collapsed = coldkey_totals(collapsed, ck_of)
        ct_naive = coldkey_totals(naive, ck_of)
        out.append("")
        out.append("  Sybil resistance (coldkeys holding >1 hotkey):")
        for ck, hks in multi.items():
            out.append(f"    {ck}: {len(hks)} hotkeys {hks}")
            out.append(f"      collapsed total weight = {ct_collapsed[ck]:.4f}   "
                       f"(naive/no-collapse would be {ct_naive[ck]:.4f}, "
                       f"~{ct_naive[ck]/max(ct_collapsed[ck],1e-9):.1f}x) "
                       f"-> splitting gains nothing")

    out.append("")
    sv = result.signed_vector
    ok = verify_vector(sv)
    out.append(f"  signed weight vector: policy_version={sv['policy_version']} "
               f"netuid={sv['netuid']} entries={len(sv['weights'])} "
               f"sig_verifies={ok}")
    for w in sorted(sv["weights"], key=lambda w: -w["weight"]):
        out.append(f"    {w['miner_hotkey']:14s} {w['weight']:.4f}")
    out.append("=" * 78)
    return "\n".join(out)
