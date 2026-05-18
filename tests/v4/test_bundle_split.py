"""Wire boundary between MinerBundle and PublisherHandle.

Finding 2 (PR #133 review): the previous ``build_miner_bundle``
returned a single ``dict`` containing both ``broken_state`` and
``clean_state``. A transport that naively serialized the full
return value would have leaked the answer to the miner.

The fix splits the return into two typed objects, ``MinerBundle``
(wire-safe) and ``PublisherHandle`` (server-only). These tests
plant unique sentinel strings in the publisher-only data and
assert they do NOT appear in ``MinerBundle.model_dump_json()``.

The pattern: any future field added to ``MinerBundle`` that pulls
from the clean state, the winning patch, or the hidden test would
make one of these assertions fail loudly.
"""

from __future__ import annotations

from cathedral.v4 import MinerBundle
from cathedral.v4.cathedral_engine import CathedralEngine, PublisherHandle

CLEAN_SENTINEL_LINE = "    return amount + amount * tax_rate"
BROKEN_SENTINEL_LINE = "    return amount - amount * tax_rate"


def test_miner_bundle_json_excludes_clean_state(engine: CathedralEngine) -> None:
    """MinerBundle.model_dump_json must NOT carry the pre-bug expression.

    Scenario: scramble the python_fastapi_base vault, then apply a
    bug patch that replaces a distinctive clean line with a broken
    one. The clean line is the sentinel: it MUST appear in
    ``handle.clean_state`` (publisher-only) and MUST NOT appear in
    ``bundle.model_dump_json()`` (miner-facing).
    """
    base_repo = "python_fastapi_base"
    seed = 4242

    scrambled = engine._scrambler.scramble(
        base_repo, seed=seed, workspace_root=engine._workspace_root
    )
    src = scrambled.files["app/calculator.py"]
    lines = src.splitlines()
    target_idx = next(i for i, ln in enumerate(lines) if CLEAN_SENTINEL_LINE in ln)
    bug_patch = (
        "--- a/app/calculator.py\n"
        "+++ b/app/calculator.py\n"
        f"@@ -{target_idx + 1},1 +{target_idx + 1},1 @@\n"
        f"-{CLEAN_SENTINEL_LINE}\n"
        f"+{BROKEN_SENTINEL_LINE}\n"
    )

    bundle, handle = engine.build_bundle_and_handle(base_repo, bug_patch=bug_patch, seed=seed)

    bundle_json = bundle.model_dump_json()

    # The wire-facing JSON must NOT carry the clean expression.
    assert CLEAN_SENTINEL_LINE not in bundle_json, (
        "MinerBundle.model_dump_json leaked the publisher-only clean expression"
    )
    # The broken expression IS in the bundle (that's the whole point).
    assert BROKEN_SENTINEL_LINE in bundle_json

    # The bundle must also not declare any of the publisher-only keys.
    for forbidden in ("clean_state", "rename_map", "file_rename_map", "string_rotation"):
        assert f'"{forbidden}"' not in bundle_json, (
            f"MinerBundle JSON carries forbidden publisher key {forbidden!r}"
        )

    # The publisher handle DOES carry the clean state -- by design.
    assert isinstance(handle, PublisherHandle)
    assert CLEAN_SENTINEL_LINE in handle.clean_state["app/calculator.py"]


def test_publisher_handle_is_not_a_minerbundle_subtype() -> None:
    """A transport that filters by type must not be fooled.

    The split only protects callers if ``PublisherHandle`` is NOT a
    subclass of ``MinerBundle`` (and vice versa). Pin it.
    """
    assert not issubclass(PublisherHandle, MinerBundle)
    assert not issubclass(MinerBundle, PublisherHandle)


def test_minerbundle_schema_excludes_clean_fields() -> None:
    """The Pydantic schema itself must not declare publisher fields."""
    schema = MinerBundle.model_json_schema()
    props = set(schema.get("properties", {}).keys())
    for forbidden in (
        "clean_state",
        "rename_map",
        "file_rename_map",
        "string_rotation",
        "winning_patch",
        "hidden_test_code",
    ):
        assert forbidden not in props, f"MinerBundle schema declares forbidden field {forbidden!r}"


def test_build_miner_bundle_returns_only_minerbundle(engine: CathedralEngine) -> None:
    """The convenience method must return JUST a MinerBundle.

    A reader of the engine API who calls ``build_miner_bundle`` must
    not be tempted to dereference a clean_state attribute. The type
    system enforces this by returning ``MinerBundle`` exclusively.
    """
    base_repo = "python_fastapi_base"
    seed = 7777
    scrambled = engine._scrambler.scramble(
        base_repo, seed=seed, workspace_root=engine._workspace_root
    )
    src = scrambled.files["app/calculator.py"]
    lines = src.splitlines()
    target_idx = next(i for i, ln in enumerate(lines) if CLEAN_SENTINEL_LINE in ln)
    bug_patch = (
        "--- a/app/calculator.py\n"
        "+++ b/app/calculator.py\n"
        f"@@ -{target_idx + 1},1 +{target_idx + 1},1 @@\n"
        f"-{CLEAN_SENTINEL_LINE}\n"
        f"+{BROKEN_SENTINEL_LINE}\n"
    )

    bundle = engine.build_miner_bundle(base_repo, bug_patch=bug_patch, seed=seed)
    assert isinstance(bundle, MinerBundle)
    # The attribute lookups PublisherHandle exposes must not exist
    # on MinerBundle.
    assert not hasattr(bundle, "clean_state")
    assert not hasattr(bundle, "rename_map")
    assert not hasattr(bundle, "winning_patch")
