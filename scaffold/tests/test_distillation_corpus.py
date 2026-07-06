"""Unit tests for Stage 4 corpus assembly."""
from __future__ import annotations

import pytest

from scaffold.distillation import RedactionPolicy, export_trace
from scaffold.lanes.audit_arena import (
    AuditTarget,
    AuditTask,
    MinerAuditSubmission,
    fixedpoint_fee_silent_zero_replay,
    sha256_text,
    verify_and_replay,
)
from scaffold.distillation_corpus import (
    CorpusConfig,
    NotAnExportError,
    UnsafeExportError,
    assemble_corpus,
    training_safe_view,
)


def _export(seq: int, accepted: bool, policy: RedactionPolicy | None = None) -> dict:
    cnf = "p cnf 1 1\n1 0\n"
    target = AuditTarget(
        target_id=f"t{seq}", repo_url="https://x.invalid/r", commit=f"sc{seq}",
        netuid=9, validator_entrypoint="v", scoring_entrypoint="s",
    )
    task = AuditTask(
        task_id=f"a{seq}", target=target, invariant_id=f"I{seq}", invariant="x",
        challenge_id=f"c{seq}", cnf_sha256=sha256_text(cnf),
        decode_map={"allow_static_witness": True, "witness": {"amount": 1, "fee_rate": 49152}},
        replay_kind="corpus_smoke", severity_hint="high",
    )
    sol = "s SATISFIABLE\nv 1 0\n" if accepted else "s SATISFIABLE\nv -1 0\n"
    v = verify_and_replay(
        task,
        MinerAuditSubmission(task_id=task.task_id, miner_hotkey=f"5H{seq}",
                             dimacs_solution=sol, agent_trace={"n": "x"}),
        cnf_text=cnf, replay_fn=fixedpoint_fee_silent_zero_replay,
    )
    return export_trace(v.distillation_trace, policy or RedactionPolicy())


def _exports(n: int = 30) -> list[dict]:
    return [_export(i, i % 3 != 0) for i in range(1, n + 1)]


def test_training_safe_view_rejects_raw_trace():
    raw = {"schema_version": "cathedral.audit_trace.v1", "submission": {}}
    with pytest.raises(NotAnExportError):
        training_safe_view(raw)


def test_training_safe_view_rejects_unsafe_export():
    unsafe = _export(1, True, RedactionPolicy(audience="private", include_raw_witness=True))
    with pytest.raises(UnsafeExportError):
        training_safe_view(unsafe)


def test_training_safe_view_strips_repo_commit():
    m = training_safe_view(_export(1, True))
    # repo_url/commit are excluded entirely from the whitelisted member.
    assert "repo_url" not in m["task"]
    assert "commit" not in m["task"]
    assert m["source_schema_version"] == "cathedral.audit_trace.v1"
    assert m["source_trace_hash"]


def test_training_safe_view_whitelists_fields():
    # A crafted export with flags false but raw material in free-text fields
    # must not leak it through (whitelist + clean-assertion).
    import pytest as _pytest
    from scaffold.distillation_corpus import UnsafeExportError as _UEE

    good = _export(1, False)
    # Inject a raw URL into a field the whitelist does NOT copy; result stays clean.
    good = dict(good)
    good["submission"] = dict(good.get("submission", {}))
    good["submission"]["agent_trace"] = {"leak": "https://evil.example/secret"}
    # Recompute export_hash so it passes the integrity check, simulating a
    # legitimately-signed-but-dangerous private export.
    import hashlib as _h, json as _j
    body = {k: v for k, v in good.items() if k != "export_hash"}
    good["export_hash"] = _h.sha256(
        _j.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    m = training_safe_view(good)
    blob = _j.dumps(m, default=str).lower()
    assert "https://" not in blob
    assert "agent_trace" not in m["submission"]  # only agent_trace_hash allowed


def test_member_set_hash_order_independent():
    exp = _exports()
    a = assemble_corpus(exp)
    b = assemble_corpus(list(reversed(exp)))
    assert a.member_set_hash == b.member_set_hash


def test_corpus_hash_binds_split_config():
    exp = _exports()
    a = assemble_corpus(exp)
    b = assemble_corpus(exp, config=CorpusConfig(split_salt="other"))
    assert a.member_set_hash == b.member_set_hash
    assert a.corpus_hash != b.corpus_hash


def test_split_stable_and_no_leakage():
    exp = _exports()
    a = assemble_corpus(exp)
    b = assemble_corpus(exp)
    assert a.split_assignments == b.split_assignments
    assert len(a.split_assignments) == len(a.members)
    for split in ("train", "val", "test"):
        for m in a.members_for_split(split):
            assert a.split_assignments[m["export_hash"]] == split


def test_public_corpus_requires_disclosure_gate():
    with pytest.raises(UnsafeExportError):
        assemble_corpus(_exports(), config=CorpusConfig(audience="public"))


def test_keeps_negative_controls():
    corpus = assemble_corpus(_exports())
    neg = [m for m in corpus.members if not m["supervision"].get("accepted")]
    assert neg


def test_dedup_drops_duplicates():
    exp = _exports(10)
    corpus = assemble_corpus(exp + exp)  # each export twice
    assert len(corpus.members) == 10
    assert corpus.drops["duplicates"] == 10
