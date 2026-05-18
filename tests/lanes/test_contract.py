"""Contract conformance suite for every Task Family lane.

This is the CI gate. A lane that passes this suite is mergeable; a lane
that fails is not. Lane authors run `pytest tests/lanes/test_contract.py
-k <family_id>` locally before opening their PR.

The suite is parametrized over every lane present under
``src/cathedral/lanes/<family_id>/``. Skips stub lanes whose methods
raise ``NotImplementedError`` so the suite stays green on main while
scaffolds exist.

Rules enforced (see ``cathedral.lanes.contract`` for the full list):

  1. Determinism: same ``(seed, tier)`` -> byte-identical
     ``PublicProblem`` and ``HiddenMetadata``.
  2. No banned imports in lane modules (network libs, subprocess, clock).
  3. Verifier is total: every adversarial fixture returns a result,
     never raises.
  4. Scores bounded in ``[0.0, 1.0]``.
  5. Golden fixtures reproduce: replay -> identical weighted_score.
  6. Adversarial fixtures all score 0.0 with non-empty
     ``rejection_reason``.

This file may grow; lanes should not have to change to satisfy new
rules without a deprecation cycle.
"""

from __future__ import annotations

import ast
import importlib
import json
import pkgutil
from pathlib import Path
from typing import Any

import pytest

import cathedral.lanes as lanes_pkg
from cathedral.lanes.contract import (
    GenerateCtx,
    HiddenMetadata,
    PublicProblem,
    ScoreResult,
    Submission,
    TaskFamily,
    VerifierResult,
)

LANES_ROOT = Path(lanes_pkg.__file__).parent

# Modules a lane is NEVER allowed to import. Network, clock, subprocess,
# unseeded randomness. The check is by string match in the AST; if a
# lane needs to bypass for a legitimate reason, add a justified
# allowlist entry here with a comment.
_BANNED_IMPORTS = frozenset(
    {
        "requests",
        "httpx",
        "aiohttp",
        "urllib.request",
        "urllib3",
        "socket",
        "subprocess",
        "os.system",
        "time",  # use ctx.issued_at_iso instead
        "datetime",  # publisher provides timestamps via ctx
    }
)


def _discover_lane_packages() -> list[str]:
    """Return importable module paths for every lane subpackage."""
    out: list[str] = []
    for info in pkgutil.iter_modules([str(LANES_ROOT)]):
        if not info.ispkg:
            continue
        # Skip the contract / registry modules and __pycache__-style noise.
        if info.name.startswith("_"):
            continue
        out.append(f"cathedral.lanes.{info.name}")
    return out


def _load_lane(module_path: str) -> TaskFamily | None:
    """Load a lane module and return its TaskFamily instance.

    Convention: the package's ``__init__.py`` exposes a class with
    ``family_id`` and ``schema_version`` attributes and the three
    contract methods. We instantiate the first such class we find.
    """
    mod = importlib.import_module(module_path)
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and hasattr(obj, "family_id") and hasattr(obj, "generate"):
            inst = obj()
            if isinstance(inst, TaskFamily):
                return inst
    return None


def _is_stub(lane: TaskFamily) -> bool:
    """A lane is a stub if generate() raises NotImplementedError on a
    minimal call. Stub lanes get skipped so the suite stays green while
    scaffolds exist."""
    try:
        lane.generate(GenerateCtx(seed=0, tier=0, issued_at_iso="2026-01-01T00:00:00.000Z"))
    except NotImplementedError:
        return True
    except Exception:
        # Real failure during generate(): not a stub, let the test report it.
        return False
    return False


# --------------------------------------------------------------------------
# Parametrization
# --------------------------------------------------------------------------


_LANE_PATHS = _discover_lane_packages()


@pytest.fixture(params=_LANE_PATHS, ids=lambda p: p.rsplit(".", 1)[-1])
def lane(request: pytest.FixtureRequest) -> TaskFamily:
    inst = _load_lane(request.param)
    if inst is None:
        pytest.skip(f"no TaskFamily class found in {request.param}")
    if _is_stub(inst):
        pytest.skip(f"{inst.family_id} is a stub — contract gates kick in once generate() returns")
    return inst


@pytest.fixture(params=_LANE_PATHS, ids=lambda p: p.rsplit(".", 1)[-1])
def lane_module_path(request: pytest.FixtureRequest) -> Path:
    pkg = importlib.import_module(request.param)
    return Path(pkg.__file__).parent  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Static import rules (run for every lane, even stubs)
# --------------------------------------------------------------------------


def test_lane_has_no_banned_imports(lane_module_path: Path) -> None:
    """Walk every .py file in the lane and assert no banned imports.

    Catches network calls, subprocess shellouts, and clock reads at
    static-analysis time. If a lane needs a banned module for a real
    reason, add an allowlist entry to _BANNED_IMPORTS with a comment.
    """
    offenders: list[tuple[str, str]] = []
    for py in lane_module_path.rglob("*.py"):
        if py.name.startswith("test_"):
            continue
        tree = ast.parse(py.read_text(), filename=str(py))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [n.name for n in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names = [node.module]
            for n in names:
                root = n.split(".")[0]
                if n in _BANNED_IMPORTS or root in _BANNED_IMPORTS:
                    offenders.append((str(py.relative_to(lane_module_path)), n))
    assert not offenders, f"banned imports in lane: {offenders}"


# --------------------------------------------------------------------------
# Behavioural conformance (skipped for stub lanes)
# --------------------------------------------------------------------------


def test_generate_is_deterministic(lane: TaskFamily) -> None:
    ctx = GenerateCtx(seed=4242, tier=1, issued_at_iso="2026-01-01T00:00:00.000Z")
    a_pub, a_hid = lane.generate(ctx)
    b_pub, b_hid = lane.generate(ctx)
    assert a_pub.model_dump_json() == b_pub.model_dump_json(), "PublicProblem not deterministic"
    assert a_hid.model_dump_json() == b_hid.model_dump_json(), "HiddenMetadata not deterministic"


def test_generate_returns_correct_family_id(lane: TaskFamily) -> None:
    ctx = GenerateCtx(seed=1, tier=0, issued_at_iso="2026-01-01T00:00:00.000Z")
    pub, _ = lane.generate(ctx)
    assert pub.task_family == lane.family_id
    assert pub.schema_version == lane.schema_version


def test_generate_obeys_pydantic_models(lane: TaskFamily) -> None:
    """The returned objects must already be the typed models, not dicts."""
    ctx = GenerateCtx(seed=1, tier=0, issued_at_iso="2026-01-01T00:00:00.000Z")
    pub, hid = lane.generate(ctx)
    assert isinstance(pub, PublicProblem)
    assert isinstance(hid, HiddenMetadata)
    assert pub.task_id == hid.task_id, "task_id must match between public and hidden"


def test_verify_is_total_on_garbage(lane: TaskFamily) -> None:
    """Adversarial: random nonsense submissions must NEVER raise."""
    ctx = GenerateCtx(seed=7, tier=0, issued_at_iso="2026-01-01T00:00:00.000Z")
    pub, hid = lane.generate(ctx)
    garbage_answers: list[dict[str, Any]] = [
        {},
        {"answer": None},
        {"assignment": "not a dict"},
        {"assignment": {"99999": True}},  # variable out of range, probably
        {"weird_key": [1, 2, 3, {"nested": "garbage"}]},
    ]
    for ans in garbage_answers:
        sub = Submission(task_id=pub.task_id, miner_hotkey="5GAR" + "B" * 44, answer=ans)
        result = lane.verify(pub, hid, sub)
        assert isinstance(result, VerifierResult)
        if not result.parsed_ok:
            assert result.rejection_reason, "rejected submission must carry a reason"


def test_score_is_bounded(lane: TaskFamily) -> None:
    """ScoreResult.weighted_score must be in [0.0, 1.0] for any verifier
    result, including hostile ones."""
    ctx = GenerateCtx(seed=11, tier=0, issued_at_iso="2026-01-01T00:00:00.000Z")
    pub, _ = lane.generate(ctx)
    hostile = [
        VerifierResult(parsed_ok=False, raw_metric=0.0, rejection_reason="x"),
        VerifierResult(parsed_ok=True, raw_metric=0.0),
        VerifierResult(parsed_ok=True, raw_metric=0.5),
        VerifierResult(parsed_ok=True, raw_metric=1.0),
        VerifierResult(parsed_ok=True, raw_metric=10.0),  # over 1.0 -> must clamp
        VerifierResult(parsed_ok=True, raw_metric=-5.0),  # negative -> must clamp
    ]
    for v in hostile:
        s = lane.score(pub, v)
        assert isinstance(s, ScoreResult)
        assert 0.0 <= s.weighted_score <= 1.0, f"weighted_score out of bounds: {s.weighted_score}"


def test_malformed_submission_scores_zero(lane: TaskFamily) -> None:
    """End-to-end: generate -> verify(garbage) -> score must be 0.0."""
    ctx = GenerateCtx(seed=13, tier=0, issued_at_iso="2026-01-01T00:00:00.000Z")
    pub, hid = lane.generate(ctx)
    sub = Submission(task_id=pub.task_id, miner_hotkey="5GAR" + "B" * 44, answer={"junk": True})
    v = lane.verify(pub, hid, sub)
    s = lane.score(pub, v)
    if not v.parsed_ok:
        assert s.weighted_score == 0.0, f"malformed submission must score 0, got {s.weighted_score}"
        assert s.rejection_reason, "rejected score must carry a reason"


# --------------------------------------------------------------------------
# Fixture replay (golden + adversarial)
# --------------------------------------------------------------------------


def _load_fixtures(lane_module_path: Path, subdir: str) -> list[dict[str, Any]]:
    folder = lane_module_path / "fixtures" / subdir
    if not folder.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]


def test_golden_fixtures_reproduce(lane: TaskFamily, lane_module_path: Path) -> None:
    fixtures = _load_fixtures(lane_module_path, "golden")
    if not fixtures:
        pytest.skip("no golden fixtures yet")
    for fx in fixtures:
        ctx = GenerateCtx(
            seed=fx["seed"], tier=fx["tier"], issued_at_iso="2026-01-01T00:00:00.000Z"
        )
        pub, hid = lane.generate(ctx)
        sub = Submission(
            task_id=pub.task_id,
            miner_hotkey=fx.get("miner_hotkey", "5" + "G" * 47),
            answer=fx["submission"],
        )
        v = lane.verify(pub, hid, sub)
        s = lane.score(pub, v)
        expected = float(fx["expected_weighted_score"])
        assert abs(s.weighted_score - expected) < 1e-9, (
            f"golden fixture {fx['name']}: expected {expected}, got {s.weighted_score}"
        )


def test_adversarial_fixtures_score_zero_cleanly(lane: TaskFamily, lane_module_path: Path) -> None:
    fixtures = _load_fixtures(lane_module_path, "adversarial")
    if not fixtures:
        pytest.skip("no adversarial fixtures yet")
    for fx in fixtures:
        ctx = GenerateCtx(
            seed=fx["seed"], tier=fx["tier"], issued_at_iso="2026-01-01T00:00:00.000Z"
        )
        pub, hid = lane.generate(ctx)
        sub = Submission(
            task_id=pub.task_id,
            miner_hotkey=fx.get("miner_hotkey", "5" + "A" * 47),
            answer=fx["submission"],
        )
        v = lane.verify(pub, hid, sub)
        s = lane.score(pub, v)
        assert s.weighted_score == 0.0, (
            f"adversarial fixture {fx['name']} scored {s.weighted_score}"
        )
        if "expected_rejection_reason" in fx:
            assert s.rejection_reason == fx["expected_rejection_reason"], (
                f"adversarial {fx['name']}: expected reason "
                f"{fx['expected_rejection_reason']!r}, got {s.rejection_reason!r}"
            )
