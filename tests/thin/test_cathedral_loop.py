from copy import deepcopy
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral_loop import Cathedral, Reject, digest


PIN = digest(b"pinned")


def executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o700)
    return path


def build_loop(
    tmp_path: Path,
    *,
    quote_verifier=None,
    trainer=None,
    evaluator=None,
    licensed=True,
):
    authority = Ed25519PrivateKey.generate()
    solver = Ed25519PrivateKey.generate()
    compute = Ed25519PrivateKey.generate()
    quote_verifier = quote_verifier or (
        lambda quote, workload: {
            "valid": quote == b"quote",
            "report_data": workload,
            "measurement": "tdx-test",
        }
    )
    trainer = trainer or (lambda members, teacher, recipe: b"candidate-checkpoint")
    evaluator = evaluator or (
        lambda model: (
            [True, True, False]
            if model == b"candidate-checkpoint"
            else [True, False, False]
        )
    )
    loop = Cathedral(
        authority,
        quote_verifier,
        PIN,
        trainer,
        PIN,
        evaluator,
        PIN,
        base_model=b"base-checkpoint",
        burn="burn-hotkey",
    )
    loop.register("solver-miner", solver)
    loop.register("compute-miner", compute)
    if licensed:
        loop.license_teacher(
            "kimi-k3",
            "registry-pinned-kimi-k3",
            "revision-1",
            digest(b"reviewed-licence"),
        )
    witness = tmp_path / "witness"
    witness.write_bytes(b"physical proof input")
    vulnerable = executable(tmp_path / "vulnerable", "kill -SEGV $$\n")
    patched = executable(tmp_path / "patched", "exit 0\n")
    return loop, solver, compute, witness, vulnerable, patched


def add_work(loop, solver, witness, vulnerable, patched):
    return loop.work(
        "solver-miner",
        solver,
        7,
        digest(b"task"),
        witness,
        vulnerable,
        patched,
        trace=b"sealed reasoning trace",
    )


def test_complete_loop_is_signed_attributed_and_fail_closed(tmp_path):
    loop, solver, compute, witness, vulnerable, patched = build_loop(tmp_path)
    first_work = add_work(loop, solver, witness, vulnerable, patched)

    checkpoint = loop.distill(
        7,
        "kimi-k3",
        "compute-miner",
        compute,
        b"quote",
        {"epochs": 1, "learning_rate": "1e-5"},
    )

    assert loop.verify() is True
    assert checkpoint["claim"]["payload"]["previous"] == digest(b"base-checkpoint")
    first_vector = loop.weights(7, {"solver-miner": 1, "compute-miner": 1})["claim"][
        "payload"
    ]["vector"]
    assert first_vector == {
        "solver-miner": 450_000,
        "compute-miner": 450_000,
        "burn-hotkey": 100_000,
    }
    corpus = next(item for item in loop.log if item["claim"]["kind"] == "corpus")
    assert corpus["claim"]["payload"]["members"] == [first_work["id"]]

    second_work = add_work(loop, solver, witness, vulnerable, patched)
    assert second_work["claim"]["payload"]["model"] == digest(b"candidate-checkpoint")
    second_vector = loop.weights(7, {"solver-miner": 2, "compute-miner": 1})["claim"][
        "payload"
    ]["vector"]
    assert second_vector == {
        "solver-miner": 600_000,
        "compute-miner": 300_000,
        "burn-hotkey": 100_000,
    }


def test_patched_build_must_survive(tmp_path):
    loop, solver, _, witness, vulnerable, _ = build_loop(tmp_path)

    with pytest.raises(Reject, match="did not isolate"):
        add_work(loop, solver, witness, vulnerable, vulnerable)

    assert all(item["claim"]["kind"] != "work" for item in loop.log)


def test_missing_proof_burns_its_entitled_share(tmp_path):
    loop, solver, _, witness, vulnerable, patched = build_loop(tmp_path)
    add_work(loop, solver, witness, vulnerable, patched)

    receipt = loop.weights(7, {"solver-miner": 1, "missing-miner": 1})

    assert receipt["claim"]["payload"]["vector"] == {
        "solver-miner": 450_000,
        "missing-miner": 0,
        "burn-hotkey": 550_000,
    }
    assert loop.verify() is True


def test_old_work_cannot_be_replayed_into_a_new_epoch(tmp_path):
    loop, solver, _, witness, vulnerable, patched = build_loop(tmp_path)
    add_work(loop, solver, witness, vulnerable, patched)

    receipt = loop.weights(8, {"solver-miner": 1})

    assert receipt["claim"]["payload"]["vector"] == {
        "solver-miner": 0,
        "burn-hotkey": 1_000_000,
    }
    assert loop.verify() is True


def test_auditor_recomputes_an_authority_signed_weight_vector(tmp_path):
    loop, solver, _, witness, vulnerable, patched = build_loop(tmp_path)
    add_work(loop, solver, witness, vulnerable, patched)
    loop._append(
        "weights",
        "cathedral",
        {
            "epoch": 7,
            "entitlements": {"solver-miner": 1},
            "vector": {"solver-miner": 1_000_000, "burn-hotkey": 0},
        },
        loop.authority,
    )

    with pytest.raises(Reject, match="invalid weight vector"):
        loop.verify()


def test_unlicensed_teacher_is_rejected_before_compute(tmp_path):
    loop, solver, compute, witness, vulnerable, patched = build_loop(
        tmp_path, licensed=False
    )
    add_work(loop, solver, witness, vulnerable, patched)

    with pytest.raises(Reject, match="not licensed"):
        loop.distill(7, "kimi-k3", "compute-miner", compute, b"quote", {})

    assert all(item["claim"]["kind"] != "compute" for item in loop.log)


def test_quote_must_bind_exact_training_workload(tmp_path):
    def verifier(quote, workload):
        return {
            "valid": True,
            "report_data": digest(b"other-workload"),
        }

    loop, solver, compute, witness, vulnerable, patched = build_loop(
        tmp_path, quote_verifier=verifier
    )
    add_work(loop, solver, witness, vulnerable, patched)

    with pytest.raises(Reject, match="did not bind"):
        loop.distill(7, "kimi-k3", "compute-miner", compute, b"quote", {})

    assert all(item["claim"]["kind"] != "compute" for item in loop.log)


def test_non_improving_candidate_records_failure_without_checkpoint(tmp_path):
    def evaluator(model):
        return [True, False]

    loop, solver, compute, witness, vulnerable, patched = build_loop(
        tmp_path, evaluator=evaluator
    )
    add_work(loop, solver, witness, vulnerable, patched)

    with pytest.raises(Reject, match="did not improve"):
        loop.distill(7, "kimi-k3", "compute-miner", compute, b"quote", {})

    assert loop.log[-1]["claim"]["kind"] == "evaluation"
    assert loop.log[-1]["claim"]["payload"]["improved"] is False
    assert all(item["claim"]["kind"] != "checkpoint" for item in loop.log)
    assert loop.model == b"base-checkpoint"


def test_tampering_or_reordering_breaks_replay(tmp_path):
    loop, solver, compute, witness, vulnerable, patched = build_loop(tmp_path)
    add_work(loop, solver, witness, vulnerable, patched)
    loop.distill(7, "kimi-k3", "compute-miner", compute, b"quote", {})
    original = deepcopy(loop.log)

    loop.log[3]["claim"]["payload"]["units"] = 99
    with pytest.raises(Reject, match="broken receipt chain"):
        loop.verify()

    loop.log = original
    loop.log[3], loop.log[4] = loop.log[4], loop.log[3]
    with pytest.raises(Reject, match="broken receipt chain"):
        loop.verify()


def test_kernel_stays_exactly_100_physical_lines():
    kernel = Path(__file__).parents[2] / "cathedral_loop.py"
    assert len(kernel.read_text(encoding="utf-8").splitlines()) == 100
