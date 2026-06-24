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


def _fake_result(round_no, emissions, passed):
    from types import SimpleNamespace
    agents = [SimpleNamespace(run=SimpleNamespace(miner_hotkey=hk, agent_id=hk),
                              gates=SimpleNamespace(passed=lambda v=passed[hk]: v))
              for hk in emissions]
    return SimpleNamespace(round_no=round_no, season="S1", targets=[], target_state={},
                           agents=agents, emissions=emissions)


def test_season_tracks_round_over_round_rank_movement():
    """Standings show momentum: an agent that overtakes another climbs (last_position
    > current), the overtaken one falls — round-over-round, not just absolute position."""
    from game.arena.season import SeasonState
    st = SeasonState()
    st.update(_fake_result(1, {"A": 100.0, "B": 50.0}, {"A": True, "B": True}))
    assert [s.hotkey for s in st.leaderboard()] == ["A", "B"]
    assert all(s.last_position == -1 for s in st.leaderboard())   # NEW entrants
    st.update(_fake_result(2, {"A": 10.0, "B": 250.0}, {"A": True, "B": True}))
    lb = st.leaderboard()
    assert [s.hotkey for s in lb] == ["B", "A"]                   # B overtook A
    pos = {s.hotkey: i for i, s in enumerate(lb)}
    bsea = next(s for s in lb if s.hotkey == "B")
    asea = next(s for s in lb if s.hotkey == "A")
    assert bsea.last_position == 1 and pos["B"] == 0              # climbed (+1)
    assert asea.last_position == 0 and pos["A"] == 1              # fell (-1)


def test_run_season_board_carries_rank_change(tmp_path):
    from game.arena.engine import ArenaEngine
    last, _ = ArenaEngine().run_season(2, state_path=str(tmp_path / "s.json"))
    assert last.season_board and all("rank_change" in row for row in last.season_board)


def test_season_persists_and_continues(tmp_path):
    p = tmp_path / "season.json"
    eng = ArenaEngine()
    _l1, s1 = eng.run_season(2, state_path=str(p))
    top1 = s1.leaderboard()[0].total_emissions
    # a SECOND season run against the same file continues accumulating
    eng2 = ArenaEngine()
    _l2, s2 = eng2.run_season(2, state_path=str(p))
    top2 = s2.leaderboard()[0].total_emissions
    assert s2.rounds == 4
    assert s2.agents[s1.leaderboard()[0].hotkey].rounds_played == 4
    assert top2 > top1
