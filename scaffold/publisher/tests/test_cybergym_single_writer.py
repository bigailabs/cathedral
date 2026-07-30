"""The CyberGym lane must not add a second weight writer.

The subnet has exactly one path that can submit weights to SN39:
``scaffold.validator_thin.set_weights_on_chain``, which calls
``set_weights_extrinsic`` behind an SN39 authorization gate. Everything else in
this repository is artifact-only or hard-refuses SN39. These tests pin that
invariant against the CyberGym bridge, which composes weights and therefore sits
one step away from being turned into a writer by a well-meaning follow-up.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from scaffold import chain
from scaffold.publisher import (
    cybergym_bridge,
    cybergym_ingest,
    mechanism_cybergym_adapter,
    mechanism_weightset,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CYBERGYM_MODULES = (
    mechanism_cybergym_adapter,
    cybergym_ingest,
    cybergym_bridge,
)


def test_exactly_one_module_calls_the_weight_extrinsic():
    """Only validator_thin may reach the chain extrinsic. A new call site
    anywhere in the shipped code (scaffold/ or cathedral_thin/) is a second
    writer and must fail this test. cathedral_thin is scanned too because it
    ships its own bittensor-capable runtime (``cathedral_thin/validator.py``),
    which is a weight writer the earlier scaffold-only scan could not see."""
    hits = sorted(
        path.relative_to(_REPO_ROOT).as_posix()
        for root in ("scaffold", "cathedral_thin")
        for path in (_REPO_ROOT / root).rglob("*.py")
        if "set_weights_extrinsic" in path.read_text()
        and "/tests/" not in path.as_posix()
    )
    assert hits == ["scaffold/validator_thin.py"]


def test_legacy_thin_runtime_refuses_sn39_submission():
    """``cathedral_thin/validator.py`` ships a bittensor-capable runtime whose
    ``submit_weights`` calls ``self.subtensor.set_weights`` indirectly (through
    ``asyncio.to_thread``), so the extrinsic-symbol scan above cannot see it.
    Pin its SN39 refusal behaviourally: on netuid 39 it raises before ever
    touching the subtensor, so the CyberGym vector cannot be laundered to SN39
    through this second writer either."""
    import asyncio
    from types import SimpleNamespace

    from cathedral_thin.core import ThinSubnetError
    from cathedral_thin.validator import BittensorRuntime

    calls: list[dict] = []
    runtime = BittensorRuntime(
        wallet=object(),
        subtensor=SimpleNamespace(set_weights=lambda **kwargs: calls.append(kwargs)),
        dendrite=object(),
        netuid=39,
        mev_protection=False,
        commit_reveal_version=4,
    )
    pending = SimpleNamespace(uids=[1], weights=[1.0])
    with pytest.raises(ThinSubnetError, match="disabled on SN39"):
        asyncio.run(runtime.submit_weights(pending))
    assert calls == []


def _code_lines(module) -> list[str]:
    """Executable lines only: docstrings and comments are documentation, and a
    module is allowed to NAME the single writer while never calling it."""
    source = inspect.getsource(module)
    lines: list[str] = []
    in_doc = False
    for raw in source.splitlines():
        line = raw.strip()
        quote = '"""'
        if in_doc:
            if quote in line:
                in_doc = False
            continue
        if line.startswith(quote) or line.startswith('r' + quote):
            body = line.split(quote, 1)[1]
            if quote not in body:
                in_doc = True
            continue
        if line.startswith("#"):
            continue
        lines.append(raw)
    return lines


@pytest.mark.parametrize(
    "module", _CYBERGYM_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1]
)
def test_cybergym_modules_contain_no_weight_writer(module):
    code = "\n".join(_code_lines(module))
    for forbidden in (
        "set_weights_extrinsic",
        "set_weights_on_chain",
        "mechanism_weightset",
        "broadcast_fn",
        "subtensor",
    ):
        assert forbidden not in code, f"{module.__name__} references {forbidden}"


@pytest.mark.parametrize(
    "module", _CYBERGYM_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1]
)
def test_cybergym_modules_import_no_chain_surface(module):
    """None of the lane's modules may pull in a chain-capable module, so no
    import-time path exists from the lane to a submission."""
    code = "\n".join(_code_lines(module))
    assert "import bittensor" not in code
    assert "from bittensor" not in code
    assert "validator_thin" not in code
    assert "from ..chain" not in code


def test_artifact_only_weight_stage_still_refuses_sn39():
    """The legacy mechanism weight stage stays hard-refusing, so composing a
    CyberGym vector can never be laundered into an SN39 submission through it."""
    with pytest.raises(mechanism_weightset.UnsafeNetworkError):
        mechanism_weightset.set_weights(
            {1: 1.0}, netuid=39, network="test", signing_key_hex="11" * 32,
        )
    with pytest.raises(mechanism_weightset.UnsafeNetworkError):
        mechanism_weightset.set_weights(
            {1: 1.0}, netuid=1, network="finney", signing_key_hex="11" * 32,
        )


def test_artifact_only_weight_stage_never_broadcasts():
    called: list[dict] = []
    out = mechanism_weightset.set_weights(
        {1: 1.0},
        netuid=1,
        network="test",
        signing_key_hex="11" * 32,
        confirm=True,
        broadcast_fn=called.append,
    )
    assert out["mode"] == "dry_run"
    assert out["broadcast"] is False
    assert called == []


def test_legacy_scaffold_chain_still_refuses_sn39():
    client = chain.ChainClient(network="test", netuid=39, broadcast=True)
    result = client.set_weights(chain.WeightVector(by_uid={1: 1.0}, by_label={}))
    assert result["submitted"] is False
    assert "SN39" in result["reason"]


def test_bridge_returns_weights_and_never_submits(tmp_path, monkeypatch):
    """The bridge's whole output is data. It has no submit parameter and no
    callback, so a caller has to go through the one real writer on purpose."""
    signature = inspect.signature(cybergym_bridge.cybergym_allocation)
    for forbidden in ("broadcast", "submit", "confirm", "signing_key_hex"):
        assert forbidden not in signature.parameters
