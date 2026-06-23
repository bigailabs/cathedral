"""Anti-cheat + fairness assertions for the local Cathedral game.

One game is run once (sandbox spawns are ~0.5s each) and shared across tests via
a module-scoped fixture. Every assertion maps to a rule in GAME_SPEC.md.
"""
from __future__ import annotations

import pytest

from game.engine import GameEngine
from game.reward import coldkey_totals, naive_weights, verify_vector


@pytest.fixture(scope="module")
def game():
    return GameEngine().run(rounds=1)


def _by_arch(game, arch):
    return next(r for r in game.results.values() if r.archetype == arch)


def _credits(game, arch):
    return [c for c in game.credits if c.hotkey == arch]


# -- the metric is real: honest work is credited -----------------------------

def test_honest_fast_wins_clean(game):
    hf = _by_arch(game, "honest_fast")
    assert hf.live
    assert all(c.verified for c in hf.credits)          # every own solve verifies
    assert all(c.contrib > 0 for c in hf.credits)       # nothing gated
    assert hf.weight > 0


def test_speed_orders_honest_miners(game):
    fast = _by_arch(game, "honest_fast")
    slow = _by_arch(game, "honest_slow")
    assert slow.weight > 0
    assert fast.weight > slow.weight                    # faster solver earns more


# -- boolean gate: deterministic SAT verification (wrong answer) --------------

def test_cheater_scores_zero(game):
    ch = _by_arch(game, "cheater_wrong")
    assert ch.weight == 0.0
    assert ch.metric == 0.0
    assert all(not c.verified for c in ch.credits)      # complete-but-wrong rejected
    assert all(c.contrib == 0.0 for c in ch.credits)


# -- boolean gate: anti-copy (a stolen answer fits a different CNF) -----------

def test_copier_scores_zero(game):
    cp = _by_arch(game, "copier")
    assert cp.weight == 0.0
    assert all(not c.verified for c in cp.credits)
    # the copier actually submitted a *satisfying* assignment — for the victim's
    # CNF, not its own — so the rejection is the witness check, not a parse error.
    assert any("witness" in (c.reason or "") for c in cp.credits)


# -- boolean gate: liveness ---------------------------------------------------

def test_dead_miner_gated_out(game):
    dead = _by_arch(game, "dead")
    assert dead.live is False
    assert dead.weight == 0.0
    assert dead.reward == 0.0


# -- boolean gate: attested compute as a gated resource ----------------------

def test_unprovisioned_earns_floor_not_premium(game):
    up = _by_arch(game, "unprovisioned")
    t1 = [c for c in up.credits if c.tier == 1]
    t2 = [c for c in up.credits if c.tier == 2]
    assert t1 and all(c.verified and c.contrib > 0 for c in t1)   # tier-1 floor earned
    assert t2 and all(c.verified for c in t2)                     # it DID solve tier-2
    assert all(not c.attest_gate and c.contrib == 0.0 for c in t2)  # but gated: no slot
    assert 0 < up.weight < _by_arch(game, "honest_fast").weight


# -- tier weighting: harder tier pays more -----------------------------------

def test_tier2_outweighs_tier1(game):
    hf = _by_arch(game, "honest_fast")
    t1 = next(c for c in hf.credits if c.tier == 1)
    t2 = next(c for c in hf.credits if c.tier == 2)
    assert t2.tier_weight > t1.tier_weight
    assert t2.contrib > t1.contrib


# -- Sybil resistance: splitting one operator gains nothing -------------------

def test_sybil_collapse_caps_coldkey(game):
    ck_of = {hk: r.coldkey for hk, r in game.results.items()}
    collapsed = {hk: r.weight for hk, r in game.results.items()}
    naive = naive_weights(game.results)
    ct_collapsed = coldkey_totals(collapsed, ck_of)
    ct_naive = coldkey_totals(naive, ck_of)
    # the sybil coldkey runs 2 hotkeys doing ~2x work; naive pay ~2x, collapse caps it
    assert ct_collapsed["ck_sybil"] <= 1.0 + 1e-6
    assert ct_naive["ck_sybil"] > ct_collapsed["ck_sybil"] * 1.5


# -- emission: the signed vector is independently verifiable ------------------

def test_signed_vector_verifies(game):
    assert verify_vector(game.signed_vector)
    hks = {w["miner_hotkey"] for w in game.signed_vector["weights"]}
    assert "cheater_wrong" not in hks and "dead" not in hks and "copier" not in hks
    assert "honest_fast" in hks


# -- the rule holds exactly: reward == liveness x sum(contrib) ----------------

def test_reward_equals_metric_times_gate(game):
    for r in game.results.values():
        metric = sum(c.contrib for c in r.credits)
        assert r.metric == pytest.approx(metric, abs=1e-9)
        assert r.reward == pytest.approx((1.0 if r.live else 0.0) * metric, abs=1e-9)
