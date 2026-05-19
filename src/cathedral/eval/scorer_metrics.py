"""Lightweight in-process counters and gauges for the v2 scorer path.

This module avoids a Prometheus dependency by exposing simple module-level
dictionaries that callers (and tests) can read and reset. Each counter is
keyed by a label string so operators and tests can distinguish the cause
of a bump (for example, the exception class on an audit write failure).

The gauges and counters here are intentionally low-cardinality and only
cover the v2 scorer cleanup rollup (cathedralai/cathedral#156). They are
not a general-purpose metrics framework.
"""

from __future__ import annotations

from typing import Final

# Number of times a v2 audit write was swallowed, keyed by the exception
# class name that the audit writer raised (for example
# ``OperationalError`` or ``IntegrityError``). The counter records the
# class even though the eval_runs transaction continues — the durable
# audit row is the post-incident anchor for forensic review, so a non-
# zero counter is itself a signal worth alerting on.
AUDIT_FAILURE_COUNTER: Final[dict[str, int]] = {}

# 1 when CATHEDRAL_SCORER=v2 is set without the companion configuration
# the v2 path requires, 0 otherwise. A gauge rather than a counter so
# the alert clears as soon as the operator fixes the configuration. The
# label is the missing companion key so dashboards can show which flag
# needs flipping.
SCORER_MISCONFIG_GAUGE: Final[dict[str, int]] = {}


def bump_audit_failure(exception_class: str) -> None:
    """Increment the audit-failure counter for the given exception class."""
    AUDIT_FAILURE_COUNTER[exception_class] = AUDIT_FAILURE_COUNTER.get(exception_class, 0) + 1


def set_scorer_misconfig(label: str, value: int) -> None:
    """Set the v2 scorer misconfiguration gauge for the given label."""
    SCORER_MISCONFIG_GAUGE[label] = value


def reset_for_tests() -> None:
    """Clear every counter and gauge. Test-only entry point."""
    AUDIT_FAILURE_COUNTER.clear()
    SCORER_MISCONFIG_GAUGE.clear()


__all__ = [
    "AUDIT_FAILURE_COUNTER",
    "SCORER_MISCONFIG_GAUGE",
    "bump_audit_failure",
    "reset_for_tests",
    "set_scorer_misconfig",
]
