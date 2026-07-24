"""Coherent launch profiles instead of per-feature env sprawl.

One operator-facing knob selects a self-consistent mechanism configuration.
Fine-grained CATHEDRAL_V2_* flags still exist for tests and surgical rollout,
but deployments should set exactly one CATHEDRAL_LAUNCH_PROFILE and stop.

Profiles:
  (unset)        -- legacy behavior, byte-identical: every feature keeps its
                    own default (off unless its env says otherwise).
  v2-converged   -- the single unified miner protocol:
                    * V2 surface on (CATHEDRAL_V2_ENABLED)
                    * bitset submit on (CATHEDRAL_V2_SUBMIT_BITSET_ENABLED)
                    * lazy issuance on (CATHEDRAL_V2_LAZY_ISSUANCE)
                    * PM payout bridge on (CATHEDRAL_V2_PM_PAYOUT_BRIDGE)
                    * startup env pinning on (no V2 per-request env lock)
                    V1 miner routes remain edge-gated; validators consume the
                    unchanged signed weight vector.

Fail-closed: contradictory explicit env under a profile is a boot error, not a
silent precedence rule. Dangerous combos (split V2 DB with the payout bridge,
missing submit-token secret) refuse to boot.
"""
from __future__ import annotations

import os

PROFILE_ENV = "CATHEDRAL_LAUNCH_PROFILE"
V2_CONVERGED = "v2-converged"
_KNOWN_PROFILES = {"", V2_CONVERGED}
_FALSY = {"0", "false", "no", "off"}


def profile() -> str:
    return os.environ.get(PROFILE_ENV, "").strip().lower()


def converged() -> bool:
    return profile() == V2_CONVERGED


def validate_env(*, signing_key_hex_provided: bool = False) -> list[str]:
    """Return fatal misconfiguration errors for the active profile."""
    errors: list[str] = []
    p = profile()
    if p not in _KNOWN_PROFILES:
        errors.append(
            f"unknown {PROFILE_ENV}={p!r}; known: {sorted(_KNOWN_PROFILES - {''})}")
        return errors
    if not converged():
        return errors

    def _explicit_off(name: str) -> bool:
        return os.environ.get(name, "").strip().lower() in _FALSY

    for name in (
        "CATHEDRAL_V2_ENABLED",
        "CATHEDRAL_V2_SUBMIT_BITSET_ENABLED",
        "CATHEDRAL_V2_LAZY_ISSUANCE",
        "CATHEDRAL_V2_PM_PAYOUT_BRIDGE",
    ):
        if _explicit_off(name):
            errors.append(
                f"{name} is explicitly off but {PROFILE_ENV}={V2_CONVERGED} "
                "implies it on; remove the override or drop the profile")
    if not os.environ.get("CATHEDRAL_V2_SUBMIT_TOKEN_SECRET", "").strip():
        errors.append(
            f"{PROFILE_ENV}={V2_CONVERGED} requires CATHEDRAL_V2_SUBMIT_TOKEN_SECRET")
    if (not signing_key_hex_provided
            and not os.environ.get("CATHEDRAL_EVAL_SIGNING_KEY", "").strip()):
        errors.append(
            f"{PROFILE_ENV}={V2_CONVERGED} requires CATHEDRAL_EVAL_SIGNING_KEY; "
            "do not launch with a generated dev key because validators pin the "
            "weight-signing identity")
    if not (os.environ.get("CATHEDRAL_V2_PERMINER_SEED_SECRET", "").strip()
            or os.environ.get("CATHEDRAL_PERMINER_SEED_SECRET", "").strip()):
        errors.append(
            f"{PROFILE_ENV}={V2_CONVERGED} requires a stable per-miner seed "
            "(CATHEDRAL_V2_PERMINER_SEED_SECRET or CATHEDRAL_PERMINER_SEED_SECRET); "
            "an ephemeral per-process seed would fork instance derivation across "
            "processes and epochs")
    if _explicit_off("CATHEDRAL_V2_PERMINER_ENABLED"):
        errors.append(
            f"CATHEDRAL_V2_PERMINER_ENABLED is explicitly off but "
            f"{PROFILE_ENV}={V2_CONVERGED} implies the per-miner surface on")
    if (os.environ.get("CATHEDRAL_V2_DATABASE_URL", "").strip()
            or os.environ.get("CATHEDRAL_V2_DB_PATH", "").strip()):
        errors.append(
            f"{PROFILE_ENV}={V2_CONVERGED} implies the payout bridge, which "
            "requires V2 to share the main store; unset "
            "CATHEDRAL_V2_DATABASE_URL/CATHEDRAL_V2_DB_PATH")
    return errors
