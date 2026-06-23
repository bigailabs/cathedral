"""Game engine — runs N rounds, wires miners + publisher + reward, returns a
result the scoreboard renders. Each round uses a fresh epoch so challenges
rotate (no replay), and credits accumulate across rounds.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import config, reward
from .miner import MinerEnv, default_roster
from .publisher import Publisher
from .reward import MinerResult, SolveCredit


@dataclass
class GameResult:
    rounds: int
    results: dict[str, MinerResult]
    credits: list[SolveCredit]
    signed_vector: dict
    live: dict[str, tuple[bool, str]] = field(default_factory=dict)


class GameEngine:
    def __init__(self, roster: list[MinerEnv] | None = None,
                 base_epoch: int = config.GAME_EPOCH):
        self.roster = roster if roster is not None else default_roster()
        self.base_epoch = base_epoch

    def run(self, rounds: int = 1, now_s: int = 0) -> GameResult:
        all_credits: list[SolveCredit] = []
        for r in range(rounds):
            pub = Publisher(epoch=self.base_epoch + r)
            all_credits.extend(pub.grade_round(self.roster, now_s))

        live = {m.hotkey: Publisher().poll_liveness(m, now_s) for m in self.roster}
        miners_meta = [
            (m.hotkey, m.coldkey, m.archetype, live[m.hotkey][0]) for m in self.roster
        ]
        results = reward.compose(miners_meta, all_credits)
        signed = reward.sign_vector(results, policy_version=self.base_epoch + rounds)
        return GameResult(rounds=rounds, results=results, credits=all_credits,
                          signed_vector=signed, live=live)
