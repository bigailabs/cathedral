"""Compatibility shim for the installed SN39 reproduction module."""

from scaffold import sn39_public_reproduction as _implementation


def __getattr__(name: str):
    return getattr(_implementation, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_implementation)))


if __name__ == "__main__":
    raise SystemExit(_implementation.main())
