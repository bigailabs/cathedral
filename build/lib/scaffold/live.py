"""Live activity runner — drives the tripartite continuously and emits a
high-signal EVENT FEED the dashboard tails: every mint, miner post, validation
verdict, consensus call, and weight update, as it happens.

    python -m scaffold.live            # run forever, ~1 round / 3s
    python -m scaffold.live 2          # period 2s

Writes data/events.jsonl (rolling) + data/harness_state.json (aggregates for the
charts). Stdlib only; solving via cryptominisat5 if present else toy DPLL.

run_forever is the loop; one round = _run_round, which threads a small `agg`
(lifetime aggregates) through focused stages: grade → consensus → accumulate →
rail/specimen → snapshot.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .contract import GenerateCtx
from . import consensus, registry, timing, specimen
import scaffold.harness as H

EVENTS = Path("data/events.jsonl")
STATE = Path("data/harness_state.json")
LEARN = Path("data/learnings.json")
MAX_EVENTS = 500
ISO = "2026-06-05T00:00:00Z"
EXPLOITS = ("liar", "fraud", "vacuous", "crier", "missed")


def _append(evs: list[dict]) -> None:
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    with open(EVENTS, "a") as f:
        for e in evs:
            f.write(json.dumps(e) + "\n")
    lines = EVENTS.read_text().splitlines()        # bound the file
    if len(lines) > MAX_EVENTS:
        EVENTS.write_text("\n".join(lines[-MAX_EVENTS:]) + "\n")


def _pool(problem):
    return [s for s in (H._submission(w, problem) for w in H.ROSTER
                        if w[1] == problem.task_family) if s]


def _rail_desc(fam: str, pi: dict) -> str:
    if fam == "encoding_v1":
        m, w = pi.get("mutation", "?"), pi.get("width", "?")
        tg = pi.get("trigger", {})
        return ("prove in-band safe (faithful round-trip)" if m == "none"
                else f"find bug · {m} @ width {w} · trigger rarity k={tg.get('k')}/{w}")
    if fam == "sat_challenge_v1":
        return f"solve CNF {pi.get('n_vars','?')}v/{pi.get('n_clauses','?')}c — fastest valid witness wins"
    if fam == "solver_docker_v1":
        return "attested solve in a TDX container — runner+solver split"
    return pi.get("task", "")


def _grade_lane(lane, problem, hidden, ts, short, agg, evs):
    """Run the roster's submissions through verify+score. Emits post/verify
    events; returns (graded, sat_answer) where graded is [(hk, vr, sr)]."""
    graded, seen, sat_answer = [], set(), None
    for sub in _pool(problem):
        if sub.task_id != problem.task_id or sub.miner_hotkey in seen:
            continue                                # task-id gate + one per hotkey
        seen.add(sub.miner_hotkey)
        evs.append({"ts": ts, "type": "post", "lane": short,
                    "worker": sub.miner_hotkey, "msg": "submitted"})
        vr = lane.validate_submission(problem, hidden, sub)
        sr = lane.score(problem, vr,
                        wall_ms=timing.observed_wall_ms(sub.miner_hotkey, problem))
        if vr.outcome.value == "sat" and sat_answer is None:
            sat_answer = sub.answer
        evs.append({"ts": ts, "type": "verify", "lane": short,
                    "worker": sub.miner_hotkey, "outcome": vr.outcome.value,
                    "score": round(sr.weighted_score, 3), "reason": sr.rejection_reason or ""})
        graded.append((sub.miner_hotkey, vr, sr))
        agg["lane_counts"][lane.family_id] = agg["lane_counts"].get(lane.family_id, 0) + 1
    return graded, sat_answer


def _consensus(lane, graded, ts, short, agg, evs):
    """Cross-miner pass: a verified find refutes 'safe' peers. Returns the
    (final, final_reason, outcome_of) maps after any overrides."""
    final = {hk: sr.weighted_score for hk, vr, sr in graded}
    final_reason = {hk: sr.rejection_reason for hk, vr, sr in graded}
    outcome_of = {hk: vr.outcome.value for hk, vr, sr in graded}
    if getattr(lane, "supports_consensus", False) and graded:
        classes = [consensus.classify(vr.outcome.value) for _, vr, _ in graded]
        for (hk, vr, sr), cv in zip(graded, consensus.resolve(classes)):
            if cv.flag and cv.flag != "n/a":
                agg["cflags"][cv.flag] = agg["cflags"].get(cv.flag, 0) + 1
            if cv.override_score is not None:
                final[hk] = cv.override_score
                final_reason[hk] = cv.reason or final_reason[hk]
                evs.append({"ts": ts, "type": "consensus", "lane": short,
                            "worker": hk, "flag": cv.flag, "msg": cv.reason or cv.flag})
            elif cv.flag == "verified_find":
                evs.append({"ts": ts, "type": "consensus", "lane": short,
                            "worker": hk, "flag": cv.flag, "msg": "verified find"})
    return final, final_reason, outcome_of


def _accumulate(graded, final, final_reason, outcome_of, agg, ts, short, evs):
    """Fold the round's scores into cumulative weight, gate counts, and the
    cheat instrumentation (bypass must stay 0)."""
    cum, gates, cheat = agg["cum"], agg["gates"], agg["cheat"]
    for hk, vr, sr in graded:
        cum[hk] = cum.get(hk, 0.0) + final[hk]
        if final[hk] == 0.0 and final_reason[hk]:
            gates[final_reason[hk]] = gates.get(final_reason[hk], 0) + 1
        if any(e in hk.lower() for e in EXPLOITS):
            cheat["attempts"] += 1
            if final[hk] <= 0.0:
                cheat["blocked"] += 1
            elif outcome_of[hk] == "sat":
                cheat["legit_find"] += 1            # earned via a VERIFIED artifact (not cheating)
            else:
                cheat["bypass"] += 1                # earned with NO verified artifact -> must stay 0
    if cheat["bypass"]:
        evs.append({"ts": ts, "type": "cheat", "lane": short,
                    "msg": "!!! BYPASS — exploit earned without a verified artifact"})


def _rail_update(lane, problem, hidden, graded, final, final_reason, outcome_of,
                 sat_answer, short, agg, rs):
    """Per-rail live stats + the latest worked specimen for the board."""
    fam, pi = lane.family_id, problem.public_input
    rb = agg["rails"].setdefault(fam, {"name": short, "desc": "", "mints": 0,
                                       "posts": 0, "finds": 0, "blocked": 0,
                                       "safe": 0, "timeout": 0, "refuted": 0,
                                       "earn_round": 0, "top": 0.0})
    rb["desc"] = _rail_desc(fam, pi)
    rb["mints"] += 1
    rb["posts"] += len(graded)
    rs["graded"] += len(graded)
    round_top, round_earn = 0.0, 0
    for hk, vr, sr in graded:
        oc = outcome_of[hk]
        if oc == "sat":
            rb["finds"] += 1
        elif oc == "unsat":
            rb["safe"] += 1
        elif oc == "timeout":
            rb["timeout"] += 1
        if final[hk] <= 0.0 and final_reason[hk] and oc == "invalid":
            rb["blocked"] += 1
        if final_reason[hk] == "refuted_by_peer_counterexample":
            rb["refuted"] += 1
        if final[hk] > 0.0:
            round_earn += 1
            round_top = max(round_top, final[hk])
            rs["earners"].add(hk)
    rb["earn_round"] = round_earn
    rb["top"] = round(round_top, 3)
    try:                                            # only showcase REAL solved examples
        if fam == "encoding_v1" and hidden.hidden_payload.get("witness"):
            agg["specimens"]["encode"] = specimen.encode_specimen(pi, hidden.hidden_payload.get("witness"))
        elif fam == "sat_challenge_v1" and sat_answer:
            agg["specimens"]["solve"] = specimen.solve_specimen(pi, sat_answer.get("assignment"))
        elif fam == "solver_docker_v1":
            agg["specimens"]["improve"] = specimen.improve_specimen(rb)
    except Exception:
        pass


def _snapshot(rnd, agg, earners) -> None:
    cum = agg["cum"]
    tot = sum(cum.values()) or 1.0
    cheat = agg["cheat"]
    learnings = {
        "uptime_rounds": rnd, "uptime_minutes": round((time.time() - agg["started"]) / 60, 1),
        "total_validations": sum(agg["lane_counts"].values()),
        "mutations_encoded": agg["mutations"], "cheat": cheat,
        "cheat_verdict": "NO CHEAT (0 bypass earnings)" if cheat["bypass"] == 0
                         else "!!! BYPASS DETECTED",
    }
    state = {
        "provenance": {"validator_label": "tripartite-vali", "repo": "cathedral-scaffold",
                       "commit": "live", "hotkey": "sim-shared-hk", "netuid": "—",
                       "network": "stitch-live", "broadcast_enabled": False},
        "metagraph": {"available": False, "reason": "live local feed"},
        "rounds": rnd, "workers": len(H.ROSTER),
        "per_worker_score": dict(sorted(cum.items(), key=lambda kv: -kv[1])),
        "lane_throughput": agg["lane_counts"], "gates_fired": agg["gates"],
        "consensus_flags": agg["cflags"], "rails": agg["rails"], "specimens": agg["specimens"],
        "attest": {"cap": 0, "live_calls": 0, "live_verified": 0, "cost_usd": 0.0},
        "weight_vector_by_worker": {k: round(v / tot, 4) for k, v in
                                    sorted(cum.items(), key=lambda kv: -kv[1])[:8] if v > 0},
        "weight_vector_on_chain_uids": {}, "submit_result": {"dry_run": True},
        "growth": agg["growth"][-30:], "learnings": learnings,
    }
    STATE.write_text(json.dumps(state, indent=2))
    LEARN.write_text(json.dumps(learnings, indent=2))


def _run_round(lanes, per_lane, rnd, agg) -> None:
    ts = time.strftime("%H:%M:%S")
    evs: list[dict] = [{"ts": ts, "type": "round", "msg": f"round {rnd}"}]
    rs = {"earners": set(), "graded": 0}            # per-round movement
    for lane in lanes:
        short = lane.family_id.replace("_v1", "")
        for _ in range(per_lane):
            ctx = GenerateCtx(seed=agg["seed"], tier=rnd % 5, issued_at_iso=ISO)  # vary width
            problem, hidden = lane.mint_challenge(ctx)
            agg["seed"] += 1
            mut = problem.public_input.get("mutation")
            if mut:
                agg["mutations"][mut] = agg["mutations"].get(mut, 0) + 1
            evs.append({"ts": ts, "type": "mint", "lane": short,
                        "msg": f"minted {problem.task_id[:10]}" + (f" [{mut}]" if mut else "")})
            graded, sat_answer = _grade_lane(lane, problem, hidden, ts, short, agg, evs)
            final, final_reason, outcome_of = _consensus(lane, graded, ts, short, agg, evs)
            _accumulate(graded, final, final_reason, outcome_of, agg, ts, short, evs)
            _rail_update(lane, problem, hidden, graded, final, final_reason,
                         outcome_of, sat_answer, short, agg, rs)
    earners = sum(1 for v in agg["cum"].values() if v > 0)
    evs.append({"ts": ts, "type": "weights", "msg": f"weight vector updated · {earners} earners"})
    agg["growth"].append({"round": rnd, "graded_total": sum(agg["lane_counts"].values()),
                          "earning_workers": earners, "graded_round": rs["graded"],
                          "active_round": len(rs["earners"])})
    _append(evs)
    _snapshot(rnd, agg, earners)


def run_forever(period: float = 3.0, per_lane: int = 1) -> None:
    registry.install_default_lanes()
    lanes = registry.active()
    agg = {"cum": {}, "lane_counts": {}, "gates": {}, "cflags": {}, "growth": [],
           "mutations": {}, "rails": {}, "specimens": {},
           "cheat": {"attempts": 0, "blocked": 0, "legit_find": 0, "bypass": 0},
           "started": time.time(), "seed": 5000}
    rnd = 0
    print("live runner started; writing events.jsonl + learnings.json", flush=True)
    while True:
        rnd += 1
        try:
            _run_round(lanes, per_lane, rnd, agg)
        except Exception as e:
            _append([{"ts": time.strftime("%H:%M:%S"), "type": "error", "msg": repr(e)[:140]}])
        time.sleep(period)


if __name__ == "__main__":
    p = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    run_forever(period=p)
