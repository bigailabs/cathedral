"""Seasons — cross-round persistence so the arena accrues history: cumulative
emissions, breach counts, verified streaks, and a season leaderboard that
survives across `python -m game.arena` runs. This is what turns a single round
into a living competition.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import economy


@dataclass
class AgentSeason:
    agent_id: str
    hotkey: str
    total_emissions: float = 0.0
    breaches: int = 0
    rounds_played: int = 0
    rounds_verified: int = 0
    streak: int = 0          # consecutive verified rounds (resets on a rejected round)
    best_streak: int = 0

    @property
    def rank(self) -> str:
        return economy.rank_for(self.total_emissions)


@dataclass
class TargetSeason:
    netuid: int
    name: str = ""
    breaches: int = 0            # cumulative verified breaches across the season
    attempts: int = 0
    first_broken_round: int | None = None

    @property
    def status(self) -> str:
        if self.breaches > 0:
            return "conquered"
        return "probed" if self.attempts > 0 else "untouched"


@dataclass
class SeasonState:
    season: str = "S1"
    rounds: int = 0
    agents: dict[str, AgentSeason] = field(default_factory=dict)
    targets: dict[int, TargetSeason] = field(default_factory=dict)

    def conquered(self) -> int:
        return sum(1 for t in self.targets.values() if t.breaches > 0)

    def update(self, result) -> None:
        """Fold one round's ArenaResult into the cumulative season."""
        self.rounds = max(self.rounds, result.round_no)
        self.season = result.season
        # cumulative subnet conquest: a target broken once stays conquered.
        for t in result.targets:
            ts = self.targets.get(t.netuid) or TargetSeason(t.netuid, t.name)
            st = result.target_state.get(t.netuid)
            if st is not None:
                ts.attempts += len(st.agents)
                if st.verified_proofs > 0:
                    ts.breaches += st.verified_proofs
                    if ts.first_broken_round is None:
                        ts.first_broken_round = result.round_no
            self.targets[t.netuid] = ts
        for a in result.agents:
            hk = a.run.miner_hotkey
            st = self.agents.get(hk) or AgentSeason(a.run.agent_id, hk)
            verified = a.gates.passed()
            st.rounds_played += 1
            st.total_emissions = round(st.total_emissions + result.emissions.get(hk, 0.0), 1)
            if verified:
                st.rounds_verified += 1
                st.breaches += 1
                st.streak += 1
                st.best_streak = max(st.best_streak, st.streak)
            else:
                st.streak = 0
            self.agents[hk] = st

    def leaderboard(self) -> list[AgentSeason]:
        return sorted(self.agents.values(),
                      key=lambda s: (-s.total_emissions, -s.best_streak, s.agent_id))

    # -- persistence ---------------------------------------------------------
    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(
            {"season": self.season, "rounds": self.rounds,
             "agents": {hk: asdict(s) for hk, s in self.agents.items()},
             "targets": {str(n): asdict(t) for n, t in self.targets.items()}}, indent=1))

    @classmethod
    def load(cls, path: str | Path) -> "SeasonState":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            d = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return cls()
        st = cls(season=d.get("season", "S1"), rounds=int(d.get("rounds", 0)))
        for hk, a in d.get("agents", {}).items():
            st.agents[hk] = AgentSeason(**{k: a[k] for k in a if k in AgentSeason.__annotations__})
        for n, t in d.get("targets", {}).items():
            st.targets[int(n)] = TargetSeason(**{k: t[k] for k in t
                                                 if k in TargetSeason.__annotations__})
        return st
