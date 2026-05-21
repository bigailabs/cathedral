"""Publisher-side launch preflight for the SAT task-family lane."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cathedral.lanes.challenge_ops import build_synthetic_boolean_challenge_record
from cathedral.lanes.challenge_source import CHALLENGE_STATUS_ACTIVE, ChallengeSourceError

ACTIVE_CNF_PATH_ENV = "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH"
CHALLENGE_ID_ENV = "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_CHALLENGE_ID"
TIER_ENV = "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_TIER"
LEGACY_TIER_ENV = "CATHEDRAL_TASK_FAMILY_TIER"
MAX_CNF_BYTES_ENV = "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_MAX_CNF_BYTES"
EVAL_SIGNING_KEY_ENV = "CATHEDRAL_EVAL_SIGNING_KEY"
WEIGHT_POLICY_SIGNING_KEY_ENV = "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY"

DEFAULT_SYNTHETIC_BOOLEAN_MAX_CNF_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class SatLaunchPreflightResult:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def read_operator_cnf_file(path: str, max_bytes: int) -> str:
    expanded = Path(path).expanduser()
    if expanded.stat().st_size > max_bytes:
        raise ValueError(f"{ACTIVE_CNF_PATH_ENV} exceeds {MAX_CNF_BYTES_ENV}")
    text = expanded.read_text(encoding="utf-8")
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError(f"{ACTIVE_CNF_PATH_ENV} exceeds {MAX_CNF_BYTES_ENV}")
    return text


def positive_int_env(env: Mapping[str, str], name: str, default: int) -> tuple[int, str | None]:
    raw = env.get(name, "").strip()
    if not raw:
        return default, None
    try:
        return max(1, int(raw)), None
    except ValueError:
        return default, f"{name} is not an integer; using default {default}"


def _hex_seed_is_32_bytes(value: str) -> bool:
    try:
        return len(bytes.fromhex(value.strip())) == 32
    except ValueError:
        return False


def run_synthetic_boolean_launch_preflight(
    env: Mapping[str, str] | None = None,
    *,
    require_eval_signing_key: bool = True,
    require_weight_signing_key: bool = True,
) -> SatLaunchPreflightResult:
    """Validate the operator-controlled SAT launch inputs without mutating state."""

    env = os.environ if env is None else env
    errors: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {}

    max_cnf_bytes, max_warning = positive_int_env(
        env, MAX_CNF_BYTES_ENV, DEFAULT_SYNTHETIC_BOOLEAN_MAX_CNF_BYTES
    )
    if max_warning:
        warnings.append(max_warning)
    details["max_cnf_bytes"] = max_cnf_bytes

    cnf_path = env.get(ACTIVE_CNF_PATH_ENV, "").strip()
    if not cnf_path:
        errors.append(f"{ACTIVE_CNF_PATH_ENV} is required")
    else:
        try:
            expanded = Path(cnf_path).expanduser()
            details["cnf_file_bytes"] = expanded.stat().st_size
            cnf_text = read_operator_cnf_file(cnf_path, max_cnf_bytes)
            tier_raw = env.get(TIER_ENV) or env.get(LEGACY_TIER_ENV, "0")
            try:
                tier = max(0, int(tier_raw))
            except ValueError:
                errors.append(f"{TIER_ENV} must be an integer")
                tier = 0
            details["tier"] = tier

            challenge_id = env.get(CHALLENGE_ID_ENV, "").strip() or None
            record = build_synthetic_boolean_challenge_record(
                cnf_text=cnf_text,
                tier=tier,
                challenge_id=challenge_id,
                status=CHALLENGE_STATUS_ACTIVE,
                source="operator_cnf_path",
            )
            details["challenge_id"] = record.challenge_id
            details["cnf_sha256"] = record.audit_metadata["cnf_sha256"]
            details["num_vars"] = record.audit_metadata["num_vars"]
            details["num_clauses"] = record.audit_metadata["num_clauses"]
        except UnicodeDecodeError:
            errors.append(f"{ACTIVE_CNF_PATH_ENV} must be UTF-8 text")
        except OSError:
            errors.append(f"{ACTIVE_CNF_PATH_ENV} could not be read")
        except ValueError as exc:
            errors.append(str(exc))
        except ChallengeSourceError as exc:
            errors.append(str(exc))

    eval_key = env.get(EVAL_SIGNING_KEY_ENV, "").strip()
    if require_eval_signing_key and not eval_key:
        errors.append(f"{EVAL_SIGNING_KEY_ENV} is required")
    elif eval_key and not _hex_seed_is_32_bytes(eval_key):
        errors.append(f"{EVAL_SIGNING_KEY_ENV} must be a 32-byte Ed25519 seed hex")

    weight_key = env.get(WEIGHT_POLICY_SIGNING_KEY_ENV, "").strip()
    if require_weight_signing_key and not weight_key:
        errors.append(
            f"{WEIGHT_POLICY_SIGNING_KEY_ENV} is required for signed remote weights"
        )
    elif weight_key and not _hex_seed_is_32_bytes(weight_key):
        errors.append(f"{WEIGHT_POLICY_SIGNING_KEY_ENV} must be a 32-byte Ed25519 seed hex")

    return SatLaunchPreflightResult(
        errors=tuple(errors),
        warnings=tuple(warnings),
        details=details,
    )


__all__ = [
    "ACTIVE_CNF_PATH_ENV",
    "CHALLENGE_ID_ENV",
    "DEFAULT_SYNTHETIC_BOOLEAN_MAX_CNF_BYTES",
    "EVAL_SIGNING_KEY_ENV",
    "LEGACY_TIER_ENV",
    "MAX_CNF_BYTES_ENV",
    "SatLaunchPreflightResult",
    "TIER_ENV",
    "WEIGHT_POLICY_SIGNING_KEY_ENV",
    "positive_int_env",
    "read_operator_cnf_file",
    "run_synthetic_boolean_launch_preflight",
]
