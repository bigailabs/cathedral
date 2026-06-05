"""Live activity runner — drives the tripartite continuously and emits a
high-signal EVENT FEED the dashboard tails: every mint, miner post, validation
verdict, consensus call, and weight update, as it happens.

    python -m scaffold.live            # run forever, ~1 round / 3s
    python -m scaffold.live 2          # period 2s

Writes data/events.jsonl (rolling) + data/harness_state.json (aggregates for the
charts). Stdlib only; solving via cryptominisat5 if present else toy DPLL.
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


def _append(evs: list[dict]) -> None:
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    with open(EVENTS, "a") as f:
        for e in evs:
            f.write(json.dumps(e) + "\n")
    # bound the file
    lines = EVENTS.read_text().splitlines()
    if len(lines) > MAX_EVENTS:
        EVENTS.write_text("\n".join(lines[-MAX_EVENTS:]) + "\n")


def run_forever(period: float = 3.0, per_lane: int = 1) -> None:
    registry.install_default_lanes()
    lanes = registry.active()

    def pool(problem):
        return [s for s in (H._submission(w, problem) for w in H.ROSTER
                            if w[1] == problem.task_family) if s]

    cum: dict[str, float] = {}
    lane_counts: dict[str, int] = {}
    gates: dict[str, int] = {}
    cflags: dict[str, int] = {}
    growth: list[dict] = []
    mutations: dict[str, int] = {}
    rails: dict[str, dict] = {}            # per-rail lifetime stats for the board
    specimens: dict[str, dict] = {}        # latest worked example per rail (the showpiece)
    cheat = {"attempts": 0, "blocked": 0, "legit_find": 0, "bypass": 0}
    EXPLOITS = ("liar", "fraud", "vacuous", "crier", "missed")
    started = time.time()
    seed = 5000
    rnd = 0
    iso = "2026-06-05T00:00:00Z"
    print("live runner started; writing events.jsonl + learnings.json", flush=True)
    while True:
        rnd += 1
        try:
            ts = time.strftime("%H:%M:%S")
            evs: list[dict] = [{"ts": ts, "type": "round", "msg": f"round {rnd}"}]
            round_earners: set[str] = set()        # distinct workers earning THIS round
            round_graded = 0                        # validations THIS round
            for lane in lanes:
                short = lane.family_id.replace("_v1", "")
                wants_consensus = bool(getattr(lane, "supports_consensus", False))
                for _ in range(per_lane):
                    ctx = GenerateCtx(seed=seed, tier=rnd % 5, issued_at_iso=iso)  # vary width
                    problem, hidden = lane.mint_challenge(ctx)
                    seed += 1
                    mut = problem.public_input.get("mutation")
                    if mut:
                        mutations[mut] = mutations.get(mut, 0) + 1
                    evs.append({"ts": ts, "type": "mint", "lane": short,
                                "msg": f"minted {problem.task_id[:10]}" + (f" [{mut}]" if mut else "")})
                    graded = []
                    seen: set[str] = set()
                    sat_answer = None           # an honest winning answer, for the specimen
                    for sub in pool(problem):
                        if sub.task_id != problem.task_id or sub.miner_hotkey in seen:
                            continue            # task-id gate + one submission per hotkey
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
                                    "score": round(sr.weighted_score, 3),
                                    "reason": sr.rejection_reason or ""})
                        graded.append((sub.miner_hotkey, vr, sr))
                        lane_counts[lane.family_id] = lane_counts.get(lane.family_id, 0) + 1
                    final = {hk: sr.weighted_score for hk, vr, sr in graded}
                    final_reason = {hk: sr.rejection_reason for hk, vr, sr in graded}
                    outcome_of = {hk: vr.outcome.value for hk, vr, sr in graded}
                    if wants_consensus and graded:
                        classes = [consensus.classify(vr.outcome.value) for _, vr, _ in graded]
                        for (hk, vr, sr), cv in zip(graded, consensus.resolve(classes)):
                            if cv.flag and cv.flag != "n/a":
                                cflags[cv.flag] = cflags.get(cv.flag, 0) + 1
                            if cv.override_score is not None:
                                final[hk] = cv.override_score
                                final_reason[hk] = cv.reason or final_reason[hk]
                                evs.append({"ts": ts, "type": "consensus", "lane": short,
                                            "worker": hk, "flag": cv.flag, "msg": cv.reason or cv.flag})
                            elif cv.flag == "verified_find":
                                evs.append({"ts": ts, "type": "consensus", "lane": short,
                                            "worker": hk, "flag": cv.flag, "msg": "verified find"})
                    for hk, vr, sr in graded:
                        cum[hk] = cum.get(hk, 0.0) + final[hk]
                        if final[hk] == 0.0 and final_reason[hk]:
                            gates[final_reason[hk]] = gates.get(final_reason[hk], 0) + 1
                        if any(e in hk.lower() for e in EXPLOITS):   # cheat instrumentation
                            cheat["attempts"] += 1
                            if final[hk] <= 0.0:
                                cheat["blocked"] += 1
                            elif outcome_of[hk] == "sat":
                                cheat["legit_find"] += 1   # earned via a VERIFIED counterexample (not cheating)
                            else:
                                cheat["bypass"] += 1       # earned with NO verified artifact -> must stay 0
                    if cheat["bypass"]:
                        evs.append({"ts": ts, "type": "cheat", "lane": short,
                                    "msg": "!!! BYPASS — exploit earned without a verified artifact"})
                    # ---- per-rail live stats (what's happening inside each rail) ----
                    fam = lane.family_id
                    pi = problem.public_input
                    if fam == "encoding_v1":
                        m, w = pi.get("mutation", "?"), pi.get("width", "?")
                        tg = pi.get("trigger", {})
                        desc = ("prove in-band safe (faithful round-trip)" if m == "none"
                                else f"find bug · {m} @ width {w} · trigger rarity k={tg.get('k')}/{w}")
                    elif fam == "sat_challenge_v1":
                        desc = f"solve CNF {pi.get('n_vars','?')}v/{pi.get('n_clauses','?')}c — fastest valid witness wins"
                    elif fam == "solver_docker_v1":
                        desc = "attested solve in a TDX container — runner+solver split"
                    else:
                        desc = pi.get("task", "")
                    rb = rails.setdefault(fam, {"name": short, "desc": "", "mints": 0,
                                                "posts": 0, "finds": 0, "blocked": 0,
                                                "safe": 0, "timeout": 0, "refuted": 0,
                                                "earn_round": 0, "top": 0.0})
                    rb["desc"] = desc
                    rb["mints"] += 1
                    rb["posts"] += len(graded)
                    round_top = 0.0
                    round_earn = 0
                    round_graded += len(graded)
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
                            round_earners.add(hk)
                    rb["earn_round"] = round_earn
                    rb["top"] = round(round_top, 3)
                    # ---- worked specimen: one real solved example per rail ----
                    try:
                        if fam == "encoding_v1" and hidden.hidden_payload.get("witness"):
                            # only showcase a FOUND bug (real counterexample + proof),
                            # not a safe round — that's the wow.
                            specimens["encode"] = specimen.encode_specimen(pi, hidden.hidden_payload.get("witness"))
                        elif fam == "sat_challenge_v1" and sat_answer:
                            specimens["solve"] = specimen.solve_specimen(pi, sat_answer.get("assignment"))
                        elif fam == "solver_docker_v1":
                            specimens["improve"] = specimen.improve_specimen(rb)
                    except Exception:
                        pass
            earners = sum(1 for v in cum.values() if v > 0)
            evs.append({"ts": ts, "type": "weights", "msg": f"weight vector updated · {earners} earners"})
            growth.append({"round": rnd, "graded_total": sum(lane_counts.values()),
                           "earning_workers": earners,
                           "graded_round": round_graded,            # movement: per-round
                           "active_round": len(round_earners)})
            _append(evs)

            tot = sum(cum.values()) or 1.0
            learnings = {
                "uptime_rounds": rnd, "uptime_minutes": round((time.time() - started) / 60, 1),
                "total_validations": sum(lane_counts.values()),
                "mutations_encoded": mutations, "cheat": cheat,
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
                "lane_throughput": lane_counts, "gates_fired": gates, "consensus_flags": cflags,
                "rails": rails, "specimens": specimens,
                "attest": {"cap": 0, "live_calls": 0, "live_verified": 0, "cost_usd": 0.0},
                "weight_vector_by_worker": {k: round(v / tot, 4) for k, v in
                                            sorted(cum.items(), key=lambda kv: -kv[1])[:8] if v > 0},
                "weight_vector_on_chain_uids": {}, "submit_result": {"dry_run": True},
                "growth": growth[-30:], "learnings": learnings,
            }
            STATE.write_text(json.dumps(state, indent=2))
            LEARN.write_text(json.dumps(learnings, indent=2))
        except Exception as e:
            _append([{"ts": time.strftime("%H:%M:%S"), "type": "error", "msg": repr(e)[:140]}])
        time.sleep(period)


if __name__ == "__main__":
    p = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    run_forever(period=p)
