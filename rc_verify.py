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
ck(f"real solver earns on all buggy ({sharp}/{buggy})", sharp==buggy and buggy>0)
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

print("\nRC GATE:", "PASS ✅ all %d checks"%len(checks) if all(c for _,c in checks) else "FAIL ❌ "+str([n for n,c in checks if not c]))
