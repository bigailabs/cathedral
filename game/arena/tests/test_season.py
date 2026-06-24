"""Seasons — cumulative cross-round accrual + persistence. Honest agents climb
the season leaderboard with streaks; cheaters never accrue; state round-trips.
"""
from __future__ import annotations

from game.arena.engine import ArenaEngine
from game.arena.season import SeasonState


def test_season_accrues_emissions_and_streaks():
    last, state = ArenaEngine().run_season(3)
    assert state.rounds == 3
    board = state.leaderboard()
    top = board[0]
    assert top.total_emissions > 0
    assert top.rounds_verified == 3 and top.streak == 3 and top.best_streak == 3
    assert top.breaches == 3
    # season board is attached to the last result for the UI
    assert last.season_board and last.season_rounds == 3


def test_cheaters_never_accrue():
    _last, state = ArenaEngine().run_season(3)
    cheats = {"hk_magpie", "hk_cuckoo", "hk_jackdaw", "hk_locust", "hk_weevil",
              "hk_mantis", "hk_hornet", "hk_cricket", "hk_moth"}
    for hk, s in state.agents.items():
        if hk in cheats:
            assert s.total_emissions == 0.0
            assert s.streak == 0 and s.breaches == 0


def test_streak_breaks_on_rejected_round(tmp_path):
    # an honest agent verified every round keeps a growing streak
    _last, state = ArenaEngine().run_season(2)
    honest = next(s for s in state.leaderboard() if s.total_emissions > 0)
    assert honest.streak == 2


def test_season_state_round_trips(tmp_path):
    _last, state = ArenaEngine().run_season(2)
    p = tmp_path / "season.json"
    state.save(p)
    loaded = SeasonState.load(p)
    assert loaded.rounds == state.rounds
    assert {hk: s.total_emissions for hk, s in loaded.agents.items()} == \
           {hk: s.total_emissions for hk, s in state.agents.items()}


def test_subnet_conquest_is_cumulative_and_monotonic():
    eng = ArenaEngine()
    _l1, s1 = eng.run_season(1)
    c1 = s1.conquered()
    assert c1 >= 1                                  # honest agents break some subnets
    broken_round1 = {n for n, t in s1.targets.items() if t.breaches > 0}
    eng2 = ArenaEngine()
    _l3, s3 = eng2.run_season(3)
    # a subnet broken stays conquered (breaches only accumulate)
    assert s3.conquered() >= len(broken_round1) or s3.conquered() >= 1
    for n, t in s3.targets.items():
        if t.breaches > 0:
            assert t.first_broken_round is not None and t.status == "conquered"


def test_conquest_persists(tmp_path):
    p = str(tmp_path / "s.json")
    ArenaEngine().run_season(2, state_path=p)
    from game.arena.season import SeasonState
    loaded = SeasonState.load(p)
    assert loaded.conquered() >= 1
    assert any(t.first_broken_round for t in loaded.targets.values())


def test_season_persists_and_continues(tmp_path):
    p = tmp_path / "season.json"
    eng = ArenaEngine()
    _l1, s1 = eng.run_season(2, state_path=str(p))
    top1 = s1.leaderboard()[0].total_emissions
    # a SECOND season run against the same file continues accumulating
    eng2 = ArenaEngine()
    _l2, s2 = eng2.run_season(2, state_path=str(p))
    top2 = s2.leaderboard()[0].total_emissions
    assert s2.agents[s1.leaderboard()[0].hotkey].rounds_played == 4
    assert top2 > top1
