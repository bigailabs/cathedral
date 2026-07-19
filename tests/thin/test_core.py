from __future__ import annotations

import base64
import time
import zlib

import pytest

from cathedral_thin.core import (
    MAX_DIMACS_BYTES,
    HashRng,
    ThinSubnetError,
    build_challenge,
    coldkey_collapsed_weights,
    decode_assignment,
    decode_cnf,
    derive_challenge_seed,
    encode_assignment,
    gate_scores_by_current_verification,
    generate_ajm_cnf,
    grade_response,
    validate_challenge_envelope,
    verify_witness,
)
from cathedral_thin.protocol import SatChallenge


SECRET = bytes(range(32))


def challenge_for(miner: str, *, round_id: int = 4):
    now = int(time.time() * 1000)
    return build_challenge(
        SECRET,
        netuid=7,
        validator_hotkey="validator",
        miner_hotkey=miner,
        round_id=round_id,
        issued_at_ms=now,
        ttl_ms=30_000,
        n_vars=32,
        n_clauses=136,
    )


def model_for(miner: str, *, round_id: int = 4):
    seed = derive_challenge_seed(
        SECRET,
        netuid=7,
        validator_hotkey="validator",
        miner_hotkey=miner,
        round_id=round_id,
    )
    return generate_ajm_cnf(seed, n_vars=32, n_clauses=136)


def test_hash_rng_samples_distinct_variables():
    rng = HashRng(b"r" * 32)
    for _ in range(10_000):
        assert len(set(rng.sample3(17))) == 3


def test_ajm_generation_is_deterministic_and_two_hidden():
    cnf, model = model_for("miner-a")
    same_cnf, same_model = model_for("miner-a")
    complement = tuple(-lit for lit in model)
    assert cnf == same_cnf
    assert model == same_model
    assert verify_witness(cnf, model)
    assert verify_witness(cnf, complement)


def test_different_hotkeys_receive_different_formulas():
    challenge_a, _ = challenge_for("miner-a")
    challenge_b, _ = challenge_for("miner-b")
    assert challenge_a.cnf_sha256 != challenge_b.cnf_sha256
    assert challenge_a.challenge_id != challenge_b.challenge_id


def test_response_assignment_is_bound_into_bittensor_body_hash():
    challenge, _ = challenge_for("miner-a")
    response = SatChallenge.from_challenge(challenge)
    request_hash = response.body_hash
    response.assignment_b64 = "AA=="
    assert response.body_hash != request_hash


def test_cnf_round_trip_and_trailing_stream_rejected():
    challenge, cnf = challenge_for("miner-a")
    assert (
        decode_cnf(
            challenge.cnf_b64,
            expected_sha256=challenge.cnf_sha256,
            expected_vars=cnf.n_vars,
            expected_clauses=cnf.n_clauses,
        )
        == cnf
    )
    compressed = base64.b64decode(challenge.cnf_b64) + zlib.compress(b"junk")
    with pytest.raises(ThinSubnetError, match="trailing"):
        decode_cnf(
            base64.b64encode(compressed).decode(),
            expected_sha256=challenge.cnf_sha256,
            expected_vars=cnf.n_vars,
            expected_clauses=cnf.n_clauses,
        )


def test_decompression_bomb_is_bounded():
    bomb = base64.b64encode(zlib.compress(b"A" * (MAX_DIMACS_BYTES + 1))).decode()
    with pytest.raises(ThinSubnetError, match="exceeds"):
        decode_cnf(
            bomb,
            expected_sha256="0" * 64,
            expected_vars=3,
            expected_clauses=1,
        )


def test_assignment_requires_exact_bitset_and_zero_padding():
    assignment = [1, -2, 3, -4, 5, -6, 7, -8, 9]
    encoded = encode_assignment(assignment, n_vars=9)
    assert decode_assignment(encoded, n_vars=9) == tuple(assignment)
    bad = bytearray(base64.b64decode(encoded))
    bad[-1] |= 0b1000_0000
    with pytest.raises(ThinSubnetError, match="padding"):
        decode_assignment(base64.b64encode(bad).decode(), n_vars=9)


def test_envelope_rejects_tampering_expiry_and_wrong_transport_identity():
    challenge, _ = challenge_for("miner-a")
    validate_challenge_envelope(
        challenge,
        now_ms=challenge.issued_at_ms,
        expected_miner="miner-a",
        expected_validator="validator",
    )
    tampered = {**challenge.__dict__, "n_vars": challenge.n_vars + 1}
    with pytest.raises(ThinSubnetError, match="commitment"):
        validate_challenge_envelope(tampered, now_ms=challenge.issued_at_ms)
    with pytest.raises(ThinSubnetError, match="expired"):
        validate_challenge_envelope(challenge, now_ms=challenge.expires_at_ms + 1)
    with pytest.raises(ThinSubnetError, match="another validator"):
        validate_challenge_envelope(
            challenge,
            now_ms=challenge.issued_at_ms,
            expected_validator="attacker",
        )


def test_copy_replay_and_identity_swap_score_zero():
    challenge_a, cnf_a = challenge_for("miner-a")
    model_a_cnf, model_a = model_for("miner-a")
    assert model_a_cnf == cnf_a
    answer_a = encode_assignment(model_a, n_vars=cnf_a.n_vars)

    challenge_b, cnf_b = challenge_for("miner-b")
    score, reason = grade_response(
        challenge_b,
        cnf_b,
        response_challenge_id=challenge_b.challenge_id,
        response_miner_hotkey="miner-b",
        assignment_b64=answer_a,
        observed_ms=10,
        received_at_ms=challenge_b.issued_at_ms + 10,
        reference_ms=100,
    )
    assert (score, reason) == (0.0, "witness_failed")

    _, reason = grade_response(
        challenge_a,
        cnf_a,
        response_challenge_id="old-id",
        response_miner_hotkey="miner-a",
        assignment_b64=answer_a,
        observed_ms=10,
        received_at_ms=challenge_a.issued_at_ms + 10,
        reference_ms=100,
    )
    assert reason == "challenge_mismatch"

    _, reason = grade_response(
        challenge_a,
        cnf_a,
        response_challenge_id=challenge_a.challenge_id,
        response_miner_hotkey="miner-b",
        assignment_b64=answer_a,
        observed_ms=10,
        received_at_ms=challenge_a.issued_at_ms + 10,
        reference_ms=100,
    )
    assert reason == "miner_identity_mismatch"


def test_score_is_correctness_dominant_and_uses_only_validator_time():
    challenge, cnf = challenge_for("miner-a")
    _, model = model_for("miner-a")
    answer = encode_assignment(model, n_vars=cnf.n_vars)
    fast, _ = grade_response(
        challenge,
        cnf,
        response_challenge_id=challenge.challenge_id,
        response_miner_hotkey="miner-a",
        assignment_b64=answer,
        observed_ms=0,
        received_at_ms=challenge.issued_at_ms,
        reference_ms=100,
    )
    slow, _ = grade_response(
        challenge,
        cnf,
        response_challenge_id=challenge.challenge_id,
        response_miner_hotkey="miner-a",
        assignment_b64=answer,
        observed_ms=10_000,
        received_at_ms=challenge.issued_at_ms,
        reference_ms=100,
    )
    assert fast == 1.0
    assert 0.8 < slow < 0.81


def test_coldkey_hotkey_duplication_does_not_increase_group_budget():
    coldkeys = {"a": "cold-a", "a2": "cold-a", "b": "cold-b"}
    single = coldkey_collapsed_weights({"a": 0.9, "a2": 0.0, "b": 0.8}, coldkeys)
    duplicated = coldkey_collapsed_weights({"a": 0.9, "a2": 0.9, "b": 0.8}, coldkeys)
    assert duplicated["a"] + duplicated["a2"] == pytest.approx(single["a"])
    assert sum(duplicated.values()) == pytest.approx(1.0)


def test_historical_ema_cannot_earn_without_current_verified_work():
    gated = gate_scores_by_current_verification(
        {"online": 0.7, "coasting": 0.99},
        {"online": 0.8, "coasting": 0.0},
    )
    assert gated == {"coasting": 0.0, "online": 0.7}
    weights = coldkey_collapsed_weights(
        gated,
        {"online": "cold-online", "coasting": "cold-coasting"},
    )
    assert weights == {"online": 1.0}
