"""Publisher-side launch preflight for the SAT task-family lane."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cathedral.eval.ssh_hermes_runner import DEFAULT_TASK_FAMILY_STDOUT_LIMIT_BYTES
from cathedral.lanes.challenge_ops import build_synthetic_boolean_challenge_record
from cathedral.lanes.challenge_source import CHALLENGE_STATUS_ACTIVE, ChallengeSourceError
from cathedral.lanes.synthetic_boolean_v1 import validate_cnf_url_challenge_id
from cathedral.publisher.sat_file_challenges import build_synthetic_boolean_file_challenge_record

ACTIVE_CNF_PATH_ENV = "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH"
CHALLENGE_ID_ENV = "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_CHALLENGE_ID"
TIER_ENV = "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_TIER"
LEGACY_TIER_ENV = "CATHEDRAL_TASK_FAMILY_TIER"
MAX_CNF_BYTES_ENV = "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_MAX_CNF_BYTES"
STORAGE_MODE_ENV = "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_STORAGE_MODE"
EVAL_SIGNING_KEY_ENV = "CATHEDRAL_EVAL_SIGNING_KEY"
TASK_FAMILY_FEED_ENABLED_ENV = "CATHEDRAL_TASK_FAMILY_FEED_ENABLED"
TASK_FAMILY_IDS_ENV = "CATHEDRAL_TASK_FAMILY_IDS"
TASK_FAMILY_STDOUT_MAX_BYTES_ENV = "CATHEDRAL_TASK_FAMILY_STDOUT_MAX_BYTES"
EVAL_MODE_ENV = "CATHEDRAL_EVAL_MODE"
PROBER_VERSION_ENV = "CATHEDRAL_PROBER_VERSION"
PUBLIC_BASE_URL_ENV = "CATHEDRAL_PUBLIC_BASE_URL"
SSH_PRIVATE_KEY_ENV = "CATHEDRAL_PROBE_SSH_PRIVATE_KEY"
SSH_KEY_PATH_ENV = "CATHEDRAL_SSH_KEY_PATH"

DEFAULT_SYNTHETIC_BOOLEAN_MAX_CNF_BYTES = 64 * 1024 * 1024
STORAGE_MODE_SQLITE_TEXT = "sqlite_text"
STORAGE_MODE_FILE = "file"
SYNTHETIC_BOOLEAN_FAMILY_ID = "synthetic_boolean_v1"


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


def _storage_mode(env: Mapping[str, str]) -> tuple[str, str | None]:
    raw = env.get(STORAGE_MODE_ENV, STORAGE_MODE_SQLITE_TEXT).strip().lower()
    normalized = raw.replace("-", "_")
    if normalized in {"", STORAGE_MODE_SQLITE_TEXT, "sqlite"}:
        return STORAGE_MODE_SQLITE_TEXT, None
    if normalized in {STORAGE_MODE_FILE, "file_backed", "filesystem"}:
        return STORAGE_MODE_FILE, None
    return STORAGE_MODE_SQLITE_TEXT, (
        f"{STORAGE_MODE_ENV} must be '{STORAGE_MODE_SQLITE_TEXT}' or '{STORAGE_MODE_FILE}'"
    )


def _hex_seed_is_32_bytes(value: str) -> bool:
    try:
        return len(bytes.fromhex(value.strip())) == 32
    except ValueError:
        return False


def _env_bool(value: str) -> bool:
    # Keep this literal parser aligned with task_family_feed_enabled().
    # Accepting "1"/"yes"/"on" here would make preflight green while the
    # publisher still skips every Task Family evaluation at runtime.
    return value.strip().lower() == "true"


def _sum_decimal_digits_through(n: int) -> int:
    total = 0
    start = 1
    digits = 1
    while start <= n:
        end = min(n, (start * 10) - 1)
        total += (end - start + 1) * digits
        start *= 10
        digits += 1
    return total


def _min_sat_answer_stdout_bytes(num_vars: int) -> int:
    """Minimum stdout bytes for a complete fenced JSON DIMACS assignment.

    Hermes stdout is capped before receipt recording. This estimates the
    smallest accepted FINAL_ANSWER block for the worst valid assignment shape:
    every variable is assigned negatively, which is one byte larger per
    literal than the positive case. It intentionally ignores miner prose; the
    launch invariant is only that a terse correct answer can fit.
    """
    variable_count = max(0, int(num_vars))
    negative_literal_bytes = _sum_decimal_digits_through(variable_count) + (
        2 * variable_count
    )
    escaped_dimacs_bytes = (
        len("s SATISFIABLE\\nv ")
        + negative_literal_bytes
        + len("0\\n")
    )
    return (
        len('```FINAL_ANSWER\n{"dimacs_solution":"')
        + escaped_dimacs_bytes
        + len('"}\n```')
    )


def _validate_stdout_cap(
    *,
    num_vars: int,
    stdout_max_bytes: int,
    errors: list[str],
    details: dict[str, Any],
) -> None:
    min_stdout_bytes = _min_sat_answer_stdout_bytes(num_vars)
    details["min_sat_answer_stdout_bytes"] = min_stdout_bytes
    if stdout_max_bytes < min_stdout_bytes:
        errors.append(
            f"{TASK_FAMILY_STDOUT_MAX_BYTES_ENV}={stdout_max_bytes} is too small "
            f"for a complete DIMACS assignment with {num_vars} variables; "
            f"set it to at least {min_stdout_bytes}"
        )


def _task_family_ids(env: Mapping[str, str]) -> list[str]:
    raw = env.get(TASK_FAMILY_IDS_ENV, SYNTHETIC_BOOLEAN_FAMILY_ID)
    return [part.strip() for part in raw.split(",") if part.strip()]


def _public_base_url_error(value: str) -> str | None:
    if not value.strip():
        return f"{PUBLIC_BASE_URL_ENV} is required for SAT cnf_url prompts"
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return f"{PUBLIC_BASE_URL_ENV} must be an absolute http(s) URL"
    return None


def _ssh_probe_key_error(env: Mapping[str, str], details: dict[str, Any]) -> str | None:
    key_env_present = bool(env.get(SSH_PRIVATE_KEY_ENV, "").strip())
    key_path = env.get(SSH_KEY_PATH_ENV, "").strip()
    details["ssh_private_key_env_present"] = key_env_present
    details["ssh_key_path"] = key_path

    if key_env_present:
        # Startup materializes this env var before the ssh-probe runner is
        # constructed; accepting it here keeps preflight aligned with boot.
        return None
    if key_path and Path(key_path).expanduser().is_file():
        details["ssh_key_path_exists"] = True
        return None

    details["ssh_key_path_exists"] = False
    return (
        f"{SSH_PRIVATE_KEY_ENV} or an existing {SSH_KEY_PATH_ENV} file is required "
        "for ssh-probe v2"
    )


def _check_runtime_env(
    env: Mapping[str, str],
    errors: list[str],
    warnings: list[str],
    details: dict[str, Any],
) -> None:
    feed_enabled = _env_bool(env.get(TASK_FAMILY_FEED_ENABLED_ENV, ""))
    family_ids = _task_family_ids(env)
    eval_mode = env.get(EVAL_MODE_ENV, "").strip().lower()
    prober_version = env.get(PROBER_VERSION_ENV, "v1").strip().lower()
    public_base_url = env.get(PUBLIC_BASE_URL_ENV, "").strip()

    details["task_family_feed_enabled"] = feed_enabled
    details["task_family_ids"] = family_ids
    details["eval_mode"] = eval_mode or ""
    details["prober_version"] = prober_version
    details["public_base_url"] = public_base_url

    if not feed_enabled:
        errors.append(f"{TASK_FAMILY_FEED_ENABLED_ENV}=true is required")
    if SYNTHETIC_BOOLEAN_FAMILY_ID not in family_ids:
        errors.append(f"{TASK_FAMILY_IDS_ENV} must include {SYNTHETIC_BOOLEAN_FAMILY_ID}")
    if eval_mode != "ssh-probe":
        errors.append(f"{EVAL_MODE_ENV}=ssh-probe is required")
    if prober_version != "v2":
        errors.append(f"{PROBER_VERSION_ENV}=v2 is required")
    if eval_mode == "ssh-probe" and prober_version == "v2":
        key_error = _ssh_probe_key_error(env, details)
        if key_error:
            errors.append(key_error)

    public_url_error = _public_base_url_error(public_base_url)
    if public_url_error:
        errors.append(public_url_error)
    elif urlparse(public_base_url).scheme == "http":
        warnings.append(f"{PUBLIC_BASE_URL_ENV} uses http; production should use https")


def run_synthetic_boolean_launch_preflight(
    env: Mapping[str, str] | None = None,
    *,
    require_eval_signing_key: bool = True,
    require_runtime_env: bool = True,
) -> SatLaunchPreflightResult:
    """Validate the operator-controlled SAT launch inputs without mutating state."""

    env = os.environ if env is None else env
    errors: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {}

    if require_runtime_env:
        _check_runtime_env(env, errors, warnings, details)

    storage_mode, storage_warning = _storage_mode(env)
    if storage_warning:
        # Publisher startup rejects invalid storage modes; preflight must fail
        # the same typo instead of silently checking a different mode.
        errors.append(storage_warning)
    details["storage_mode"] = storage_mode

    max_cnf_bytes, max_warning = positive_int_env(
        env, MAX_CNF_BYTES_ENV, DEFAULT_SYNTHETIC_BOOLEAN_MAX_CNF_BYTES
    )
    if max_warning:
        warnings.append(max_warning)
    details["max_cnf_bytes"] = max_cnf_bytes
    max_cnf_bytes_configured = bool(env.get(MAX_CNF_BYTES_ENV, "").strip())
    enforce_max_cnf_bytes = storage_mode != STORAGE_MODE_FILE or max_cnf_bytes_configured
    details["max_cnf_bytes_enforced"] = enforce_max_cnf_bytes

    stdout_max_bytes, stdout_warning = positive_int_env(
        env,
        TASK_FAMILY_STDOUT_MAX_BYTES_ENV,
        DEFAULT_TASK_FAMILY_STDOUT_LIMIT_BYTES,
    )
    if stdout_warning:
        warnings.append(stdout_warning)
    details["task_family_stdout_max_bytes"] = stdout_max_bytes

    cnf_path = env.get(ACTIVE_CNF_PATH_ENV, "").strip()
    if not cnf_path:
        errors.append(f"{ACTIVE_CNF_PATH_ENV} is required")
    else:
        try:
            expanded = Path(cnf_path).expanduser()
            details["cnf_file_bytes"] = expanded.stat().st_size
            tier_raw = env.get(TIER_ENV) or env.get(LEGACY_TIER_ENV, "0")
            try:
                tier = max(0, int(tier_raw))
            except ValueError:
                errors.append(f"{TIER_ENV} must be an integer")
                tier = 0
            details["tier"] = tier

            challenge_id = env.get(CHALLENGE_ID_ENV, "").strip() or None
            if challenge_id is not None:
                # Keep launch checks aligned with cnf_url announcement: the id
                # is a route path segment, so reserved URL characters are fatal.
                validate_cnf_url_challenge_id(challenge_id)
            if storage_mode == STORAGE_MODE_FILE:
                record = build_synthetic_boolean_file_challenge_record(
                    cnf_path=cnf_path,
                    tier=tier,
                    challenge_id=challenge_id,
                    status=CHALLENGE_STATUS_ACTIVE,
                    source="operator_cnf_path",
                    max_bytes=max_cnf_bytes if enforce_max_cnf_bytes else None,
                )
            else:
                cnf_text = read_operator_cnf_file(cnf_path, max_cnf_bytes)
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
            _validate_stdout_cap(
                num_vars=int(record.audit_metadata["num_vars"]),
                stdout_max_bytes=stdout_max_bytes,
                errors=errors,
                details=details,
            )
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

    return SatLaunchPreflightResult(
        errors=tuple(errors),
        warnings=tuple(warnings),
        details=details,
    )


__all__ = [
    "ACTIVE_CNF_PATH_ENV",
    "CHALLENGE_ID_ENV",
    "DEFAULT_SYNTHETIC_BOOLEAN_MAX_CNF_BYTES",
    "EVAL_MODE_ENV",
    "EVAL_SIGNING_KEY_ENV",
    "LEGACY_TIER_ENV",
    "MAX_CNF_BYTES_ENV",
    "PROBER_VERSION_ENV",
    "PUBLIC_BASE_URL_ENV",
    "SSH_KEY_PATH_ENV",
    "SSH_PRIVATE_KEY_ENV",
    "STORAGE_MODE_ENV",
    "STORAGE_MODE_FILE",
    "STORAGE_MODE_SQLITE_TEXT",
    "TASK_FAMILY_FEED_ENABLED_ENV",
    "TASK_FAMILY_IDS_ENV",
    "TASK_FAMILY_STDOUT_MAX_BYTES_ENV",
    "TIER_ENV",
    "SatLaunchPreflightResult",
    "positive_int_env",
    "read_operator_cnf_file",
    "run_synthetic_boolean_launch_preflight",
]
