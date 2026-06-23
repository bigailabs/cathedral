"""Load the REAL audit-hunter corpus into typed arena objects.

Data sources (all real artifacts produced by prior audit work):
  * audit-hunter/registered_uids.json    — our 17 registered subnet UIDs
  * audit-hunter/val_candidates.json     — 17 candidate findings (objectives)
  * audit-hunter/resources_per_subnet.json — what's needed to EARN per subnet
  * audit-hunter/factory/out/manifest.json — subtensor money-math CNF corpus
  * audit-hunter/cnf/manifest.json       — pre-existing AMM conservation CNFs

If the audit-hunter repo is absent the loaders fall back to a small bundled demo
corpus so installed wheels still run. Repo-local runs still prefer the real
audit-hunter artifacts.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .models import ProofTask, Target

# audit-hunter lives next to the cathedral repo: .../code/audit-hunter
_AUDIT_HUNTER = Path(__file__).resolve().parents[3] / "audit-hunter"


def _load_json(rel: str, default):
    p = _AUDIT_HUNTER / rel
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _risk_for(resource: str) -> str:
    r = (resource or "").lower()
    if "none" in r and "key" in r:
        return "local-replay"
    if "script" in r or "cpu" in r or "none" in r:
        return "sandbox-replay"
    if "token" in r or "key" in r or "api" in r:
        return "testnet"
    return "read-only-mainnet"


def _fallback_targets() -> list[Target]:
    """Bundled install-safe targets.

    These are not scored as historical discoveries. They keep the local game and
    scanner contract runnable when the adjacent audit-hunter repo is not present,
    which is the normal state for a wheel install.
    """
    rows = [
        (111, "oneoneone", "Volume Score Inflation", 8, 182,
         "Inflation via supply record beyond spot-check sample", "local replay"),
        (29, "Coldint", "Nan-wins-first", 6, 6,
         "Weight model wins all samples when committed earliest", "local replay"),
        (38, "ChronoLLM", "Replay probable unverifiable reference", 4, 54,
         "Replay approach submits public answer reference models", "sandbox CPU"),
        (49, "Nepher Robotics", "Custom setup blocks score", 3, 3,
         "Python package evaluation path can be made unverifiable", "sandbox CPU"),
        (60, "Bitsec.ai", "Repeated clean return supply line", 8, 8,
         "Return-supply line lets direction pass without proof", "local replay"),
        (100, "Platform", "Self-reported hotkey without signature", 7, 193,
         "Miner stores self-reported hotkey without signature verification",
         "local replay"),
        (106, "Nodexo / NI-Compute", "Python interpreter allows fake identity", 5,
         156, "Miner-controlled Python interpreter lets fake UID and FLOPs",
         "sandbox CPU"),
        (2, "DSperse", "Proof response writes wrong server file", 6, 188,
         "Unsampled proof responses can create issue without cryptographic check",
         "local replay"),
        (25, "Mainframe", "Log file accepted verbatim", 8, 68,
         "Miner-controlled log file accepted verbatim on probabilistic skip path",
         "local replay"),
        (45, "Talisman AI", "Controlled content length multiplier", 7, 79,
         "Article reward inflation through controlled content length drivers",
         "local replay"),
        (93, "Bitcast", "Prompt provides check-disabled payload", 7, 148,
         "Direct disabled validator-only checks transport a forged description",
         "local replay"),
        (44, "Score", "Private-track digest self-reported", 6, 246,
         "Miner-selected private-track digest can self-report independently",
         "local replay"),
    ]
    return sorted(
        [
            Target(
                netuid=netuid,
                name=name,
                repo="bundled-demo",
                our_uid=uid,
                candidate_title=title,
                severity=sev,
                location="bundled:fallback",
                exploit_steps=steps,
                exploit_resource=resource,
                risk_level=_risk_for(resource),
            )
            for netuid, name, title, sev, uid, steps, resource in rows
        ],
        key=lambda t: -t.severity,
    )


def _fallback_proof_tasks() -> list[ProofTask]:
    """Package-local proof tasks derived from the built-in replay harnesses."""
    from . import replay

    tasks: list[ProofTask] = []
    for target_id, target in sorted(replay.TARGETS.items()):
        tasks.append(ProofTask(
            cnf_id=target_id,
            model=target.cls,
            rule=target.family,
            family=target.family,
            invariant=target.property_desc,
            vars=len(target.decode),
            clauses=0,
            result="sat" if target.known_witness else "unknown",
            tier="demo",
            role="replay",
            triage="bundled deterministic replay harness",
            file=target.source,
            witness=target.known_witness,
        ))
    return tasks


def load_targets() -> list[Target]:
    """The 17 subnet targets, merged from the three real JSON artifacts by netuid."""
    uids = _load_json("registered_uids.json", {}).get("uids", {})
    candidates = _load_json("val_candidates.json", [])
    resources = _load_json("resources_per_subnet.json", {}).get("per_subnet", {})

    if not candidates:
        return _fallback_targets()

    # registered_uids keys look like "111_oneoneone" -> our UID; index by netuid.
    uid_by_netuid: dict[int, int] = {}
    for key, uid in uids.items():
        m = re.match(r"(\d+)_", key)
        if m:
            uid_by_netuid[int(m.group(1))] = int(uid)

    # resources keyed "2_DSperse" -> {"uid":188, "needs":[...], "external":...}
    res_by_netuid: dict[int, dict] = {}
    for key, val in resources.items():
        m = re.match(r"(\d+)_", key)
        if m:
            res_by_netuid[int(m.group(1))] = val

    targets: list[Target] = []
    for c in candidates:
        netuid = int(c["netuid"])
        res = res_by_netuid.get(netuid, {})
        resource = res.get("external") or (", ".join(res.get("needs", [])) or "unknown")
        targets.append(Target(
            netuid=netuid,
            name=c.get("name", f"sn{netuid}"),
            repo=c.get("github", ""),
            our_uid=uid_by_netuid.get(netuid),
            candidate_title=c.get("title", ""),
            severity=int(c.get("sev", 0)),
            location=c.get("location", ""),
            exploit_steps=c.get("line", ""),
            exploit_resource=c.get("res", resource),
            risk_level=_risk_for(c.get("res", "")),
        ))
    return sorted(targets, key=lambda t: -t.severity)


def load_proof_tasks() -> list[ProofTask]:
    """The subtensor money-math CNF corpus (28 minted + 2 pre-existing)."""
    man = _load_json("factory/out/manifest.json", {})
    tasks: list[ProofTask] = []
    for inst in man.get("instances", []):
        tasks.append(ProofTask(
            cnf_id=inst.get("cnf_id", "?"),
            model=inst.get("model", "?"),
            rule=inst.get("rule", "?"),
            family=inst.get("family", "?"),
            invariant=inst.get("invariant", ""),
            vars=int(inst.get("vars", 0)),
            clauses=int(inst.get("clauses", 0)),
            result=inst.get("verified_result", "unknown"),
            tier=inst.get("tier", "?"),
            role=inst.get("role", ""),
            triage=inst.get("triage", ""),
            file=inst.get("file", ""),
            witness=inst.get("witness"),
        ))
    for inst in man.get("pre_existing", []):
        tasks.append(ProofTask(
            cnf_id=inst.get("cnf_id", "?"),
            model="subtensor-amm", rule="conservation", family="A_conservation",
            invariant=inst.get("note", ""), vars=int(inst.get("vars", 0)),
            clauses=int(inst.get("clauses", 0)),
            result=inst.get("verified_result", "unknown"),
            tier="hard", role="assurance", triage=inst.get("note", ""),
            file=inst.get("file", ""), witness=inst.get("witness"),
        ))
    return tasks or _fallback_proof_tasks()


def corpus_summary() -> dict:
    targets = load_targets()
    tasks = load_proof_tasks()
    audit_hunter_present = _AUDIT_HUNTER.exists()
    return {
        "targets": len(targets),
        "proof_tasks": len(tasks),
        "sat": sum(1 for t in tasks if t.result == "sat"),
        "unsat": sum(1 for t in tasks if t.result == "unsat"),
        "unknown": sum(1 for t in tasks if t.result == "unknown"),
        "audit_hunter_present": audit_hunter_present,
        "source": "audit-hunter" if audit_hunter_present else "bundled-fallback",
    }
