"""python -m game [rounds]  — run the local Cathedral game and print the scoreboard."""
from __future__ import annotations

import sys

from .engine import GameEngine
from .scoreboard import render


def main() -> None:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    result = GameEngine().run(rounds=rounds)
    print(render(result))


if __name__ == "__main__":
    main()
