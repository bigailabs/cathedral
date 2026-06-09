"""Release-candidate gate: every load-bearing invariant across all 3 lanes, in one run."""
import sys; sys.path.insert(0,'.')
from scaffold.contract import GenerateCtx, Submission, Outcome
from scaffold import registry, timing, grading
from scaffold.dimacs import solve_cnf
import scaffold.harness as H
registry.install_default_lanes()
L={l.family_id:l for l in registry.active()}
A,B,C=L["sat_challenge_v1"],L["solver_docker_v1"],L["encoding_v1"]
checks=[]
def ck(name, cond): checks.append((name,bool(cond))); print(("  PASS " if cond else "  FAIL ")+name)

print("LANE A — solve (witness self-verifies, speed server-measured)")
p,h=A.mint_challenge(GenerateCtx(seed=7,tier=2,issued_at_iso="x")); sol=solve_cnf(p.public_input["cnf"])
ck("honest witness verifies", A.validate_submission(p,h,Submission(p.task_id,"a",{"assignment":sol})).outcome==Outcome.SAT)
ck("liar all-true rejected", A.validate_submission(p,h,Submission(p.task_id,"l",{"assignment":[1]*p.public_input['n_vars']})).outcome==Outcome.INVALID)
s_lie1=A.score(p,A.validate_submission(p,h,Submission(p.task_id,"A-honest-1",{"assignment":sol,"wall_ms":1})),wall_ms=timing.observed_wall_ms("A-honest-1",p))
s_lie2=A.score(p,A.validate_submission(p,h,Submission(p.task_id,"A-honest-1",{"assignment":sol,"wall_ms":999999})),wall_ms=timing.observed_wall_ms("A-honest-1",p))
ck("self-reported wall_ms ignored", abs(s_lie1.weighted_score-s_lie2.weighted_score)<1e-9)
fast=A.score(p,A.validate_submission(p,h,Submission(p.task_id,"f",{"assignment":sol})),wall_ms=50.0)
slow=A.score(p,A.validate_submission(p,h,Submission(p.task_id,"s",{"assignment":sol})),wall_ms=50000.0)
ck("faster server-time scores higher", fast.weighted_score>slow.weighted_score)

print("LANE B — attested solve (speed only from bound, tamper-evident elapsed)")
pb,hb=B.mint_challenge(GenerateCtx(seed=7,tier=2,issued_at_iso="x"))
r1=B.score(pb,B.validate_submission(pb,hb,H._submission(("B-runner-1","solver_docker_v1","runner"),pb)))
r2=B.score(pb,B.validate_submission(pb,hb,H._submission(("B-runner-2","solver_docker_v1","runner"),pb)))
ck("attested solve >= non-attested floor", r1.weighted_score>=r2.weighted_score and r2.weighted_score>0)
ck("attesting earns strictly more here", r1.weighted_score>r2.weighted_score)
ck("timeout fraud blocked", B.score(pb,B.validate_submission(pb,hb,H._submission(("B-fraud-4","solver_docker_v1","timeout_fraud"),pb))).weighted_score==0)
from scaffold.verify import verify_attestation, attested_elapsed_ms
from scaffold.polaris import PolarisClient
import os,base64,hashlib,dataclasses
pc=PolarisClient(live=False); nn=os.urandom(8).hex(); pk=base64.b64encode(os.urandom(32)).decode()
ok,res=verify_attestation(pc,nonce=nn,pubkey_b64=pk,expected_image="img:s",workload="solve",measured_elapsed_ms=300)
tam=dataclasses.replace(res,stdout=res.stdout.replace("elapsed_ms=300","elapsed_ms=1"))
rh=hashlib.sha256(tam.stdout.encode()).hexdigest()
ck("attested elapsed tamper-evident", ok and tam.report_data[32:64]!=hashlib.sha256((tam.image_digest+rh).encode()).digest())

print("LANE C — encode (solve-hard trigger, witness quality, no bypass)")
crier=sharp=buggy=0
for seed in range(200):
    pe,he=C.mint_challenge(GenerateCtx(seed=9000+seed,tier=seed%5,issued_at_iso="x"))
    if not he.hidden_payload["is_buggy"]: continue
    buggy+=1
    if C.score(pe,C.validate_submission(pe,he,Submission(pe.task_id,"cr",{"verdict":"bug","counterexample":0,"encode":"faithful"}))).weighted_score>0: crier+=1
    if C.score(pe,C.validate_submission(pe,he,Submission(pe.task_id,"sh",{"verdict":"bug","counterexample":he.hidden_payload["witness"],"encode":"faithful"})),wall_ms=20.0).weighted_score>0: sharp+=1
ck(f"crier const-0 never earns ({crier}/{buggy})", crier==0)
# The "real solver earns" check requires the z3 backend (a verified counterexample
# re-check). Without z3 the lane's offline fallback DELIBERATELY earns nothing on a
# bug claim (z3_required_for_verified_find), so this check is z3-gated: asserted
# when z3 is present, reported SKIP (not FAIL) otherwise. The negative gates below
# (no crier earnings, no bypass, no crashes) hold WITHOUT z3 and are always run.
from scaffold.lanes.encoding import have_z3
if have_z3():
    ck(f"real solver earns on all buggy ({sharp}/{buggy})", sharp==buggy and buggy>0)
else:
    print("  SKIP real solver earns on all buggy (z3 backend not installed)")
bypass=0; crashes=0
for seed in range(150):
    pe,he=C.mint_challenge(GenerateCtx(seed=1234+seed,tier=seed%5,issued_at_iso="x"))
    for ans in [{"verdict":"bug","counterexample":0},{"verdict":"safe","encode":"vacuous","solved":True},{"verdict":"safe","encode":"faithful","solved":True},{"verdict":"bug","counterexample":[]},{"verdict":"bug","counterexample":"x"}]:
        try:
            v=C.validate_submission(pe,he,Submission(pe.task_id,"x",ans)); sc=C.score(pe,v,wall_ms=10.0)
            if sc.weighted_score>0 and v.outcome.value!="sat": bypass+=1
        except Exception: crashes+=1
ck(f"0 exploit bypass ({bypass})", bypass==0)
ck(f"0 crashes on malformed input ({crashes})", crashes==0)

print("LANE S — solver arena (champion/challenger, PAR-2 + marginal-VBS, certified)")
from scaffold.lanes.arena_e2e import run_arena_round
from scaffold.lanes import sandbox as _sbx
from scaffold.lanes.solver_arena import (SolverRegistry, SolverSpec, ChampionMachine,
    par2_ms, marginal_vbs_count, InstanceResult)
from scaffold.contract import Outcome as _O
arun = run_arena_round(seed=4242, tier=0, batch_size=6, include_real=False)
by = {r[0]: r for r in arun.rows}
# the honest fast solver beats the seeded SC2025 champion -> record falls + earns.
ck("honest solver beats champion (record falls)", by["honest_fast"][2] > 0 and by["honest_fast"][4]["record_fell"])
ck("record-fall event emitted (jackpot/burn-step trigger)", len(arun.record_falls) == 1)
# every adversarial case scores 0 WITH a reason.
ck("liar solver (forged witness) scores 0", by["liar_solver"][2] == 0 and bool(by["liar_solver"][3]))
ck("forged DRAT scores 0", by["forged_unsat"][2] == 0 and bool(by["forged_unsat"][3]))
ck("timeout fraud scores 0", by["timeout_solver"][2] == 0 and bool(by["timeout_solver"][3]))
ck("copied champion dedups -> 0 (copy can't beat champion)",
   by["champion_copy"][2] == 0 and by["champion_copy"][3] == "already_evaluated_or_duplicate")
ck("every certified solve has a verified certificate",
   all(d[4].get("all_certified", True) for d in arun.rows))
# opportunistic: when a real solver is installed, it must earn certified credit
# and dethrone the stub champion (runs on Stitch; SKIPs where no binary).
from scaffold.lanes.arena_e2e import have_real_solver as _hrs
_real = _hrs()
if _real:
    arun2 = run_arena_round(seed=4242, tier=0, batch_size=6, include_real=True)
    by2 = {r[0]: r for r in arun2.rows}
    _row = by2[f"real_{_real[0]}"]
    ck(f"real solver ({_real[0]}) certified + dethrones stub champion",
       _row[2] > 0 and _row[4]["record_fell"] and _row[4].get("all_certified", True))
else:
    print("  SKIP real-solver dethrone (no kissat/cadical installed)")
# Sybil: k identities publishing ONE solver -> one commitment, zero marginal merit.
reg = SolverRegistry()
acc = [reg.register(SolverSpec(f"url{i}", f"sha256:{'c'*64}", "same_source_hash", f"hk{i}"))[0] for i in range(5)]
ck("sybil dedup (k identities -> 1 commitment)", sum(acc) == 1 and len(reg.all()) == 1)
# one-eval-per-commitment: a re-submission of an evaluated solver is not re-evaluated.
reg.mark_evaluated("same_source_hash")
ck("one-eval-per-commitment (no replay weight farming)", not reg.needs_eval("same_source_hash"))
# PAR-2 monotonicity: an unsolved instance costs PAR_K*timeout (penalized).
solved_fast = [InstanceResult("t1", _O.SAT, 100.0, True, "witness")]
unsolved = [InstanceResult("t1", _O.TIMEOUT, 5000.0, False, "")]
ck("PAR-2 penalizes timeouts", par2_ms(unsolved, {"t1": 5000.0}) > par2_ms(solved_fast, {"t1": 5000.0}))
# strict dethrone margin: a within-margin win does NOT topple the record.
cm = ChampionMachine(dethrone_margin_ms=1000.0)
champ = [InstanceResult("t1", _O.SAT, 2000.0, True, "witness")]
cm.seed_champion(SolverSpec("u", "d", "champ_h"), champ, {"t1": 5000.0})
near = [InstanceResult("t1", _O.SAT, 1500.0, True, "witness")]   # 500ms faster < 1000 margin
ck("strict dethrone margin holds (within-noise win keeps record)",
   cm.consider(SolverSpec("u2", "d2", "near_h"), near, {"t1": 5000.0}) is None)
far = [InstanceResult("t1", _O.SAT, 500.0, True, "witness")]     # 1500ms faster >= margin
ck("clear win dethrones past the margin", cm.consider(SolverSpec("u3", "d3", "far_h"), far, {"t1": 5000.0}) is not None)
# containment property: the eval host runs solvers network-isolated when able.
if _sbx.sandbox_available():
    rnet = _sbx.run_solver(["python3", "-c",
        "import socket;socket.create_connection(('8.8.8.8',53),timeout=2)"], timeout_s=8)
    ck("sandbox is network-isolated (solver cannot phone home)", rnet.returncode not in (0,) and rnet.contained)
else:
    print("  SKIP sandbox network isolation (no unprivileged userns on this host)")

print("LANE A — designated solver field (advisory, does not gate verify/score)")
from scaffold.lanes.sat_challenge import SatChallengeLane
DIGEST = "sha256:" + "d"*64
A2 = SatChallengeLane(designated_solver_digest=DIGEST)
pA,hA = A2.mint_challenge(GenerateCtx(seed=7,tier=0,issued_at_iso="x"))
solA = solve_cnf(pA.public_input["cnf"])
ck("designated solver advertised on public problem", pA.public_input.get("designated_solver_digest")==DIGEST)
# community submission is still graded purely on the certificate-checked witness.
ck("community witness still verifies (designation is advisory)",
   A2.validate_submission(pA,hA,Submission(pA.task_id,"m",{"assignment":solA})).outcome==Outcome.SAT)
# same (seed,tier) on the plain lane vs the designated lane -> identical score:
# the designation is metadata only, it never touches verify/score.
pP,hP = A.mint_challenge(GenerateCtx(seed=7,tier=0,issued_at_iso="x"))
solP = solve_cnf(pP.public_input["cnf"])
score_plain = A.score(pP,A.validate_submission(pP,hP,Submission(pP.task_id,"m",{"assignment":solP})),wall_ms=50.0).weighted_score
score_desig = A2.score(pA,A2.validate_submission(pA,hA,Submission(pA.task_id,"m",{"assignment":solA})),wall_ms=50.0).weighted_score
ck("designation does not change score vs plain lane", abs(score_plain-score_desig)<1e-9)

print("NON-REGRESSION — synthetic_boolean fixtures (verifier totality, bounded alloc)")
# Port of the monolith's 9 adversarial + 3 golden fixtures, re-expressed against
# Lane A's contract verify (a DIMACS 'v ...' line -> a signed-literal assignment).
def _assign_from_vline(vline):
    return [int(x) for x in vline.replace("v","").split() if x.strip("- ").isdigit() and int(x)!=0]
golden = [(1,0,"v 1 0"),(42,1,"v 1 2 3 0"),(7,2,"v 1 -2 3 4 0")]  # status SATISFIABLE
g_ok=0
for seed,tier,vline in golden:
    pg,hg = A.mint_challenge(GenerateCtx(seed=seed,tier=tier,issued_at_iso="x"))
    sol = solve_cnf(pg.public_input["cnf"])   # the REAL satisfying assignment
    vr = A.validate_submission(pg,hg,Submission(pg.task_id,"g",{"assignment":sol}))
    if vr.outcome==Outcome.SAT and A.score(pg,vr,wall_ms=10.0).weighted_score>0: g_ok+=1
ck(f"golden: honest solves score >0 ({g_ok}/3)", g_ok==3)
# adversarial: each hostile assignment must be rejected (INVALID/0), never crash.
pad,had = A.mint_challenge(GenerateCtx(seed=42,tier=1,issued_at_iso="x"))
nv = pad.public_input["n_vars"]
adversarial = [
    ("unsatisfied",      [-1,2,-3]+[1]*(nv-3)),               # complete but wrong literal set
    ("incomplete",       [1,2]),                               # too few vars
    ("contradictory",    [1,-1,2,3]+[1]*(nv-3)),               # x1 both T and F
    ("out_of_range",     [1,2,3,nv+50]+[1]*(nv-4)),            # variable past n_vars
    ("non_integer",      "not-a-list"),                        # malformed answer type
    ("empty",            []),                                  # empty assignment
]
adv_rej=0; adv_crash=0
for name,asg in adversarial:
    try:
        vr = A.validate_submission(pad,had,Submission(pad.task_id,"a",{"assignment":asg}))
        sc = A.score(pad,vr,wall_ms=10.0)
        if sc.weighted_score==0 and vr.outcome==Outcome.INVALID: adv_rej+=1
    except Exception: adv_crash+=1
ck(f"adversarial assignments all rejected ({adv_rej}/{len(adversarial)})", adv_rej==len(adversarial))
ck(f"verifier totality: 0 crashes on hostile input ({adv_crash})", adv_crash==0)
# bounded allocation: an out-of-range variable does not allocate past n_vars.
ck("bounded allocation (out-of-range var rejected, not allocated)",
   A.validate_submission(pad,had,Submission(pad.task_id,"b",{"assignment":[nv+999]})).outcome==Outcome.INVALID)

print("SHADOW — v4-vs-prod weight diff table (0.10 blocker / 0.01 investigate)")
from scaffold import shadow
# identical vectors -> all OK, advance permitted.
v_eq = shadow.evaluate({1:0.5,2:0.5}, {1:0.5,2:0.5})
ck("identical vectors -> advance OK, no blockers", v_eq.advance_ok and v_eq.blockers==0)
# a 0.02 divergence -> investigate band, still advance-OK.
v_inv = shadow.evaluate({1:0.50,2:0.50}, {1:0.52,2:0.48})
ck("0.02 divergence -> investigate (not blocker)",
   any(r.band=="investigate" for r in v_inv.rows) and v_inv.advance_ok and v_inv.blockers==0)
# a 0.20 divergence -> blocker, migration HELD.
v_blk = shadow.evaluate({1:0.50,2:0.50}, {1:0.70,2:0.30})
ck("0.20 divergence -> blocker holds the migration", v_blk.blockers>0 and not v_blk.advance_ok)
# a UID present only in shadow (gained all weight) is diffed against prod=0.
v_new = shadow.evaluate({1:1.0}, {2:1.0})
ck("UID present in only one regime diffed against 0", any(r.uid==2 and r.diff==1.0 for r in v_new.rows))
# threshold boundaries are exact (>= is the gate).
ck("threshold boundaries exact", shadow.band_for(0.10)=="blocker" and shadow.band_for(0.01)=="investigate" and shadow.band_for(0.0099)=="ok")

print("\nRC GATE:", "PASS ✅ all %d checks"%len(checks) if all(c for _,c in checks) else "FAIL ❌ "+str([n for n,c in checks if not c]))
