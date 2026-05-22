"""FastAPI app for the publisher process (api.cathedral.computer).

Owns:
- POST /v1/agents/submit + reads (cathedral.publisher.submit + reads)
- background eval orchestrator (cathedral.eval.orchestrator)
- on-demand merkle close via CLI command (cathedral.publisher.merkle)

Does NOT own:
- Bittensor weight setting (that's the validator binary)
- the existing /v1/claim Polaris-evidence flow (that's the validator
  binary's `cathedral.validator.app` - left untouched for backward
  compat with current miners until they migrate)
"""

from __future__ import annotations

import asyncio
import os
import secrets
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from cathedral.cards.registry import CardRegistry
from cathedral.eval.orchestrator import run_eval_loop
from cathedral.eval.polaris_runner import (
    BundleCardRunner,
    HttpPolarisRunner,
    HttpPolarisRunnerConfig,
    PolarisRunner,
    StubPolarisRunner,
)
from cathedral.eval.scoring_pipeline import EvalSigner
from cathedral.lanes.challenge_lock import (
    SQLITE_SCHEMA as CHALLENGE_LOCK_SCHEMA,
)
from cathedral.lanes.challenge_lock import (
    SqliteChallengeLock,
)
from cathedral.lanes.challenge_ops import seed_synthetic_boolean_challenge
from cathedral.lanes.challenge_source import (
    SQLITE_SCHEMA as CHALLENGE_SOURCE_SCHEMA,
)
from cathedral.lanes.challenge_source import (
    SqliteChallengeSource,
    SqliteFetchTokenStore,
)
from cathedral.lanes.synthetic_boolean_v1 import FAMILY_ID as SYNTHETIC_BOOLEAN_FAMILY_ID
from cathedral.publisher import repository
from cathedral.publisher.reads import router as reads_router
from cathedral.publisher.submit import router as submit_router
from cathedral.storage import HippiusClient, HippiusConfig, StubHippiusClient
from cathedral.validator.db import connect

logger = structlog.get_logger(__name__)


@dataclass
class PublisherContext:
    """Wired into `app.state.ctx`. Holds connections + background dependencies."""

    db: aiosqlite.Connection
    hippius: HippiusClient
    polaris: PolarisRunner
    signer: EvalSigner
    registry: CardRegistry
    submissions_paused: bool = False
    background_tasks: list[asyncio.Task[Any]] = field(default_factory=list)


_DEFAULT_SSH_PROBE_KEY_PATH = "/tmp/cathedral_probe_ed25519"  # noqa: S108


def _materialize_ssh_probe_key() -> None:
    """Write the platform-wide SSH private key from env to disk at startup.

    The Railway publisher container has no SSH private key baked in - it
    only ships with the *public* key as ``CATHEDRAL_PROBE_SSH_PUBLIC_KEY``.
    The ``attestation_mode=ssh-probe`` runners (``SshHermesRunner`` and
    ``SshProbeRunner``) refuse to construct if their
    ``ssh_private_key_path`` doesn't point at a real file, so without this
    helper every ssh-probe submission hangs in ``evaluating`` forever.

    Behaviour:
    - ``CATHEDRAL_PROBE_SSH_PRIVATE_KEY`` unset and no file at
      ``CATHEDRAL_SSH_KEY_PATH``: log a warning and return. Other
      attestation modes still work; only ssh-probe is degraded.
    - Env var set: write to ``CATHEDRAL_SSH_KEY_PATH`` (default
      ``/tmp/cathedral_probe_ed25519``) with ``0600`` perms.
    - File already exists with identical content: short-circuit, no
      rewrite - idempotent across restarts.
    - File exists with different content: overwrite (operator rotated
      the key - respect the new value).
    """
    target_str = os.environ.get("CATHEDRAL_SSH_KEY_PATH", _DEFAULT_SSH_PROBE_KEY_PATH)
    target = Path(target_str).expanduser()

    raw = os.environ.get("CATHEDRAL_PROBE_SSH_PRIVATE_KEY")
    if not raw:
        if not target.is_file():
            logger.warning("ssh_probe_key_missing", path=str(target))
        return

    # Some env-var transports strip trailing newlines; OpenSSH refuses
    # keys without one.
    if not raw.endswith("\n"):
        raw += "\n"

    target.parent.mkdir(parents=True, exist_ok=True)

    if target.is_file():
        try:
            existing = target.read_text()
        except OSError:
            existing = None
        if existing == raw:
            logger.info("ssh_probe_key_already_current", path=str(target))
            return

    target.write_text(raw)
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    logger.info("ssh_probe_key_materialized", path=str(target))


def build_publisher_app(ctx_factory: Any, *, start_eval_loop: bool = True) -> FastAPI:
    """Build the FastAPI app. `ctx_factory` is an async callable returning
    a `PublisherContext` - kept indirect so tests can inject mocks.

    `start_eval_loop=False` (used by `build_app` in tests) skips the
    background scheduler so tests can drive ticks deterministically via
    `cathedral.eval.orchestrator.run_once()`.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Must run BEFORE the orchestrator constructs SshHermesRunner /
        # SshProbeRunner - those raise on a missing private key file at
        # __init__, which would block every ssh-probe submission. Cheap,
        # idempotent, safe in test mode (no-op when env is unset).
        _materialize_ssh_probe_key()
        ctx: PublisherContext = await ctx_factory()
        app.state.ctx = ctx
        await ctx.db.executescript(CHALLENGE_SOURCE_SCHEMA)
        await ctx.db.executescript(CHALLENGE_LOCK_SCHEMA)
        await ctx.db.commit()
        task_family_challenge_source = SqliteChallengeSource(ctx.db)
        task_family_challenge_lock = SqliteChallengeLock(ctx.db)
        task_family_fetch_token_store = SqliteFetchTokenStore(ctx.db)
        # Stash on app.state so the public CNF route can validate
        # tokens and look up active/locked rows without re-opening
        # the connection or re-wiring through dependency injection.
        app.state.task_family_challenge_source = task_family_challenge_source
        app.state.task_family_fetch_token_store = task_family_fetch_token_store
        await _seed_synthetic_boolean_challenge_from_env(task_family_challenge_source)

        # Make ctx visible to the orchestrator's env-resolver. Production
        # `from_settings` previously skipped this; the test-only `build_app`
        # set it inside its own factory. Hoisting to the shared lifespan
        # so PolarisRuntimeRunner can find HippiusClient in both modes.
        global _LATEST_CTX
        _LATEST_CTX = ctx

        # One-shot startup repair for rows stranded in 'evaluating' with
        # a prior current_score from pre-PR-#117 behavior. Idempotent -
        # zero rows match once the live data is clean, so it stays in
        # place as a safety net for any future stranding (process
        # crashes mid-eval, etc.) without runtime cost.
        from cathedral.publisher.repository import repair_stale_evaluating_rows

        repaired = await repair_stale_evaluating_rows(ctx.db)
        if repaired:
            logger.warning("stale_evaluating_rows_repaired", count=repaired)
        else:
            logger.info("stale_evaluating_rows_repaired", count=0)

        # Preload the v3 bug-isolation private corpus so the operator can
        # confirm a non-empty corpus from boot logs BEFORE flipping
        # CATHEDRAL_V3_FEED_ENABLED. Without this, the loader is only
        # called inside the feed-gated _maybe_run_v3_bug_isolation path
        # and the operator's runbook preflight is impossible to satisfy.
        # The call is a no-op (returns ()) when CATHEDRAL_V3_CORPUS_PATH
        # is unset, which keeps test/dev runs quiet.
        if os.environ.get("CATHEDRAL_V3_CORPUS_PATH"):
            from cathedral.v3.corpus.private_loader import load_private_corpus

            load_private_corpus()

        stop = asyncio.Event()
        if start_eval_loop:
            # Per-submission runner dispatch: polaris-tier rows go to
            # PolarisRuntimeRunner (Tier A), TEE-tier rows are pre-verified
            # at submit and only need the bundled card scored, everything
            # else falls back to CATHEDRAL_EVAL_MODE.
            from cathedral.eval.orchestrator import (
                _resolve_polaris_runner_for_mode,
                _resolve_polaris_runner_from_env,
            )

            def _runner_for(submission: dict[str, Any]) -> Any:
                # Per-submission runner dispatch - the production wiring
                # that mirrors the test-friendly `runner_for` in
                # orchestrator.run_eval_loop. Order matters:
                #   1. env-mode stub overrides (tests + dev)
                #   2. attestation_mode='polaris-deploy' (v2 paid) - real
                #      Hermes via Polaris's native deploy pipeline
                #   3. attestation_mode='ssh-probe' (v2 free) - Cathedral
                #      SSHs into the miner's box
                #   4. attestation_mode='polaris' (legacy v1) - the
                #      cathedral-runtime LLM shim path, kept as backup
                #   5. attestation_mode='tee' - bundled card, pre-verified
                #   6. anything else falls back to CATHEDRAL_EVAL_MODE
                mode = (submission.get("attestation_mode") or "").lower()
                env_mode = os.environ.get("CATHEDRAL_EVAL_MODE", "").lower()
                has_key = bool(os.environ.get("POLARIS_ATTESTATION_PUBLIC_KEY"))
                if env_mode.startswith("stub"):
                    return _resolve_polaris_runner_from_env()
                if mode == "polaris-deploy" and has_key:
                    return _resolve_polaris_runner_for_mode("polaris-deploy")
                if mode == "ssh-probe":
                    return _resolve_polaris_runner_for_mode("ssh-probe")
                if mode == "polaris" and has_key:
                    return _resolve_polaris_runner_for_mode("polaris")
                if mode == "tee":
                    return _resolve_polaris_runner_for_mode("bundle")
                if mode == "bundle":
                    # BYO-compute: miner baked the produced card into the
                    # bundle at artifacts/last-card.json. Publisher reads
                    # and scores without re-running the agent.
                    return _resolve_polaris_runner_for_mode("bundle")
                return _resolve_polaris_runner_from_env()

            eval_task = asyncio.create_task(
                run_eval_loop(
                    db=ctx.db,
                    hippius=ctx.hippius,
                    runner_for=_runner_for,
                    signer=ctx.signer,
                    registry=ctx.registry,
                    poll_interval_secs=10.0,
                    max_concurrent=_env_int("CATHEDRAL_EVAL_MAX_CONCURRENT", 2),
                    stop=stop,
                    task_family_challenge_source=task_family_challenge_source,
                    task_family_challenge_lock=task_family_challenge_lock,
                    task_family_fetch_token_store=task_family_fetch_token_store,
                    public_base_url=os.environ.get("CATHEDRAL_PUBLIC_BASE_URL", "").strip() or None,
                )
            )
            ctx.background_tasks.append(eval_task)
        try:
            yield
        finally:
            stop.set()
            globals()["_LATEST_CTX"] = None
            for t in ctx.background_tasks:
                t.cancel()
            await asyncio.gather(*ctx.background_tasks, return_exceptions=True)
            await ctx.db.close()

    app = FastAPI(title="Cathedral Publisher", lifespan=lifespan)

    # CORS - cathedral.computer (static site on Cloudflare Pages) fetches
    # /v1/agents/{id} directly from the publisher to populate the agent
    # profile modal. Without these headers the in-browser fetch fails and
    # the modal can only show the seed data baked into the page (no
    # hotkey, vigil, sparkline, or recent-stones list). Allow the two
    # public origins plus the live preview deploys; reads are public so
    # the surface is intentionally permissive.
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=(
            r"^https://(cathedral\.computer|"
            r"www\.cathedral\.computer|"
            r"[a-z0-9-]+\.cathedral-site\.pages\.dev|"
            r"localhost:\d+)$"
        ),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        max_age=600,
    )

    # Always render `{"detail": "<string>"}` per CONTRACTS.md Section 9 lock #3.
    @app.exception_handler(StarletteHTTPException)
    async def _http_exc_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict):
            # Some endpoints (e.g. /health 503) intentionally pass a dict
            # body - render it directly.
            return JSONResponse(status_code=exc.status_code, content=detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(detail) if detail else exc.__class__.__name__},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Surface a single readable line; the full pydantic detail is
        # logged separately for the operator dashboard.
        first = exc.errors()[0] if exc.errors() else {"msg": "invalid request"}
        return JSONResponse(
            status_code=400,
            content={"detail": str(first.get("msg", "invalid request"))},
        )

    @app.exception_handler(HTTPException)
    async def _api_http_exc(_request: Request, exc: HTTPException) -> JSONResponse:
        # Same as Starlette handler but covers the FastAPI subclass.
        detail = exc.detail
        if isinstance(detail, dict):
            return JSONResponse(status_code=exc.status_code, content=detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(detail) if detail else "error"},
        )

    # CONTRACTS Section 2 locks the public surface at `/api/cathedral/v1/...`
    # (matches the cross-repo contract test mirror, the frontend's API client,
    # and the polariscomputer-side routes already deployed). Mount BOTH:
    # - /api/cathedral/v1/...   - canonical contract surface
    # - /v1/... (and /health)   - back-compat for direct callers + infra
    #   healthchecks (Railway, k8s) that expect `/health` at root
    # FastAPI handlers are stateless, so dual-mounting is just two route
    # entries pointing at the same function - no duplicated state.
    app.include_router(submit_router, prefix="/api/cathedral")
    app.include_router(reads_router, prefix="/api/cathedral")
    app.include_router(submit_router, include_in_schema=False)
    app.include_router(reads_router, include_in_schema=False)

    # Public CNF endpoint for the synthetic boolean lane. Same dual-mount
    # so callers using either the canonical /api/cathedral prefix or the
    # back-compat /v1 root resolve to the same route. The route is gated
    # by per-challenge fetch tokens minted at announce time; never log
    # the ?t= query string.
    from cathedral.publisher.challenge_cnf import router as challenge_cnf_router

    app.include_router(challenge_cnf_router, prefix="/api/cathedral")
    app.include_router(challenge_cnf_router, include_in_schema=False)

    # Agent-facing onboarding - Moltbook-style. A miner pastes
    # `Read https://api.cathedral.computer/skill.md and follow the
    # instructions to mine the eu-ai-act card` into their AI agent;
    # the agent fetches this URL and self-registers.
    from fastapi.responses import PlainTextResponse

    from cathedral.publisher.skill_md import SKILL_MD_CONTENT

    @app.get("/skill.md", response_class=PlainTextResponse, include_in_schema=False)
    async def _skill_md() -> PlainTextResponse:
        return PlainTextResponse(
            SKILL_MD_CONTENT,
            media_type="text/markdown; charset=utf-8",
        )

    # Cathedral's public SSH key for v2 free-tier (ssh-probe) miners.
    # Miners install this line in their box's ~/.ssh/authorized_keys for
    # the user they nominate in their submission's `ssh_user` field.
    # Source: env var CATHEDRAL_PROBE_SSH_PUBLIC_KEY. We do NOT load this
    # from disk because the publisher's Railway container doesn't get
    # the platform-wide key material baked in.
    @app.get(
        "/.well-known/cathedral-ssh-key.pub",
        response_class=PlainTextResponse,
        include_in_schema=False,
    )
    async def _ssh_pubkey() -> PlainTextResponse:
        import os as _os

        pub = _os.environ.get("CATHEDRAL_PROBE_SSH_PUBLIC_KEY", "").strip()
        if not pub:
            # Surface a clear error rather than serve an empty file -
            # miners would silently fail to authorize otherwise.
            return PlainTextResponse(
                "# Cathedral probe SSH key not yet configured on the publisher.\n"
                "# CATHEDRAL_PROBE_SSH_PUBLIC_KEY env var is empty.\n"
                "# Email ops@cathedral.computer for the canonical key while we wire this up.\n",
                status_code=503,
                media_type="text/plain; charset=utf-8",
            )
        # Always return a single-line public key + a trailing newline so
        # miners can `curl ... >> ~/.ssh/authorized_keys` directly.
        if not pub.endswith("\n"):
            pub = pub + "\n"
        return PlainTextResponse(pub, media_type="text/plain; charset=utf-8")

    # Cathedral's signing pubkey + the pinned Polaris attestation pubkey,
    # published so validators can fetch them without DMing ops. Mirrors
    # the convention referenced in cathedral.validator.pull_loop.
    #
    # Cathedral signs every EvalRun projection with this key. Validators
    # pin it once and verify the signature on `/v1/leaderboard/recent`
    # rows locally. The Polaris key is published alongside as context
    # (the publisher uses it internally to verify Polaris attestations
    # before scoring); validators do not need to verify Polaris
    # signatures themselves.
    @app.get(
        "/.well-known/cathedral-jwks.json",
        include_in_schema=False,
    )
    async def _jwks() -> dict[str, Any]:
        import base64 as _b64
        import os as _os

        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
        )

        out: dict[str, Any] = {
            "issuer": "cathedral.computer",
            "keys": [],
        }

        # Cathedral signing pubkey - derived from the private seed in
        # CATHEDRAL_EVAL_SIGNING_KEY. We never serve the private key.
        sk_hex = _os.environ.get("CATHEDRAL_EVAL_SIGNING_KEY", "").strip()
        if sk_hex:
            try:
                seed = bytes.fromhex(sk_hex)
                priv = Ed25519PrivateKey.from_private_bytes(seed)
                pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
                out["keys"].append(
                    {
                        "kid": "cathedral-eval-signing",
                        "use": "sig",
                        "alg": "EdDSA",
                        "kty": "OKP",
                        "crv": "Ed25519",
                        "x": _b64.urlsafe_b64encode(pub).rstrip(b"=").decode(),
                        "public_key_hex": pub.hex(),
                        "purpose": (
                            "Cathedral signs every EvalRun projection "
                            "served from /v1/leaderboard/recent. Pin this "
                            "key in your validator config."
                        ),
                    }
                )
            except Exception as exc:
                logger.warning("jwks_eval_signing_key_invalid", error=str(exc))

        # Polaris attestation pubkey - the key the publisher pins when
        # verifying Polaris's runtime attestations during scoring.
        polaris_hex = _os.environ.get("POLARIS_ATTESTATION_PUBLIC_KEY", "").strip()
        if polaris_hex:
            try:
                polaris_bytes = bytes.fromhex(polaris_hex)
                out["keys"].append(
                    {
                        "kid": "polaris-runtime-attestation",
                        "use": "sig",
                        "alg": "EdDSA",
                        "kty": "OKP",
                        "crv": "Ed25519",
                        "x": _b64.urlsafe_b64encode(polaris_bytes).rstrip(b"=").decode(),
                        "public_key_hex": polaris_hex,
                        "purpose": (
                            "Polaris signs runtime attestations over each "
                            "Cathedral eval. The publisher verifies before "
                            "scoring; validators do not need to verify "
                            "this signature themselves."
                        ),
                    }
                )
            except Exception as exc:
                logger.warning("jwks_polaris_key_invalid", error=str(exc))

        return out

    return app


async def _seed_synthetic_boolean_challenge_from_env(source: SqliteChallengeSource) -> None:
    """Seed one active SAT challenge from an operator-mounted CNF file.

    The file path and raw CNF stay publisher-local. This helper never logs
    the path or CNF body, and the public schema-5 row still exposes only
    hash-backed fields.
    """
    cnf_path = os.environ.get("CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH", "").strip()
    if not cnf_path:
        return

    try:
        cnf_text = await asyncio.to_thread(_read_text_file, cnf_path)
    except OSError:
        raise RuntimeError(
            "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH could not be read"
        ) from None

    challenge_id = os.environ.get("CATHEDRAL_SYNTHETIC_BOOLEAN_V1_CHALLENGE_ID", "").strip()
    tier_raw = os.environ.get("CATHEDRAL_SYNTHETIC_BOOLEAN_V1_TIER") or os.environ.get(
        "CATHEDRAL_TASK_FAMILY_TIER", "0"
    )
    try:
        tier = max(0, int(tier_raw))
    except ValueError:
        raise RuntimeError("CATHEDRAL_SYNTHETIC_BOOLEAN_V1_TIER must be an integer") from None

    try:
        active = await seed_synthetic_boolean_challenge(
            source,
            cnf_text=cnf_text,
            tier=tier,
            now_iso=_now_ms_iso(),
            challenge_id=challenge_id or None,
            activate=True,
            input_source="operator_cnf_path",
        )
    except Exception as exc:
        raise RuntimeError(str(exc)) from None

    audit = active.audit_metadata
    logger.info(
        "synthetic_boolean_active_challenge_seeded",
        family_id=SYNTHETIC_BOOLEAN_FAMILY_ID,
        tier=tier,
        num_vars=audit.get("num_vars"),
        num_clauses=audit.get("num_clauses"),
        cnf_sha256=audit.get("cnf_sha256"),
    )


def _read_text_file(path: str) -> str:
    return Path(path).expanduser().read_text(encoding="utf-8")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("invalid_int_env", env=name, value=raw, default=default)
        return default


def _now_ms_iso() -> str:
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# --------------------------------------------------------------------------
# Production wiring
# --------------------------------------------------------------------------


def from_settings(database_path: str = "data/publisher.db") -> FastAPI:
    """Build the publisher app for production use.

    Environment variables consumed:
      CATHEDRAL_KEK_HEX or CATHEDRAL_MASTER_ENCRYPTION_KEY (32-byte hex)
      CATHEDRAL_EVAL_SIGNING_KEY (32-byte hex Ed25519 private key)
      CATHEDRAL_EVAL_MODE (optional):
        "stub"   -> StubPolarisRunner (placeholder card for smoke tests)
        "bundle" -> BundleCardRunner  (BYO-compute; score miner's pre-baked
                                       artifacts/last-card.json directly)
        unset / other -> HttpPolarisRunner (talks to Polaris compute)
      HIPPIUS_S3_ACCESS_KEY / HIPPIUS_S3_SECRET_KEY / HIPPIUS_S3_ENDPOINT
        / HIPPIUS_S3_REGION / HIPPIUS_S3_BUCKET
      POLARIS_BASE_URL + POLARIS_API_TOKEN (when CATHEDRAL_EVAL_MODE is
        unset or points at the HTTP runner)
    """

    async def _factory() -> PublisherContext:
        conn = await connect(database_path)
        # Prefer real Hippius; fall back to in-memory stub when env is
        # unset OR the bucket isn't reachable. We choose RAM over hard-fail
        # because v1 launch can survive bundle loss on redeploy, but a
        # publisher that won't accept submissions is dead on arrival.
        # A startup probe (HEAD on the bucket) is what triggers the
        # fallback - config-only checks miss the common case where keys
        # parse fine but lack PutObject permission.
        hippius: Any
        try:
            client = HippiusClient(HippiusConfig.from_env())
            if not await client.healthcheck():
                raise RuntimeError("hippius healthcheck failed")
            hippius = client
        except Exception as e:
            import structlog

            structlog.get_logger(__name__).warning(
                "hippius_unavailable_falling_back_to_stub", error=str(e)
            )
            hippius = StubHippiusClient()

        signing_hex = os.environ.get("CATHEDRAL_EVAL_SIGNING_KEY")
        if not signing_hex:
            raise RuntimeError("CATHEDRAL_EVAL_SIGNING_KEY env var required (32-byte hex)")
        signer = EvalSigner.from_env_hex(signing_hex)

        polaris: PolarisRunner
        eval_mode = os.environ.get("CATHEDRAL_EVAL_MODE", "").lower()
        if eval_mode == "stub":
            polaris = StubPolarisRunner()
        elif eval_mode == "bundle":
            polaris = BundleCardRunner()
        else:
            polaris = HttpPolarisRunner(
                HttpPolarisRunnerConfig(
                    base_url=os.environ.get("POLARIS_BASE_URL", "https://api.polaris.computer"),
                    api_token=os.environ.get("POLARIS_API_TOKEN", ""),
                )
            )

        return PublisherContext(
            db=conn,
            hippius=hippius,
            polaris=polaris,
            signer=signer,
            registry=CardRegistry.baseline(),
        )

    return build_publisher_app(_factory)


# --------------------------------------------------------------------------
# Test-friendly builder
# --------------------------------------------------------------------------


# v1 launch card_ids per CONTRACTS.md §9 lock #12. Seeded into a fresh DB
# so contract tests against `eu-ai-act` etc. find a card definition.
_V1_LAUNCH_CARDS: tuple[dict[str, Any], ...] = (
    {
        "id": "eu-ai-act",
        "display_name": "EU AI Act",
        "jurisdiction": "eu",
        "topic": "EU AI Act enforcement and guidance",
    },
)

# Card IDs that were part of an earlier 5-card launch plan and have been
# deprecated. Existing production rows for these IDs are archived at
# Docker startup (see `cathedral-publisher archive-cards` in cli.py) so
# submit and eval-spec lookups return 404. Keeping the list here in code
# (not just docs) so the archival is idempotent and self-documenting.
_V1_DEPRECATED_CARD_IDS: tuple[str, ...] = (
    "us-ai-eo",
    "uk-ai-whitepaper",
    "singapore-pdpc",
    "japan-meti-mic",
)


_DEFAULT_RUBRIC: dict[str, Any] = {
    "source_quality_weight": 0.30,
    "maintenance_weight": 0.20,
    "freshness_weight": 0.15,
    "specificity_weight": 0.15,
    "usefulness_weight": 0.10,
    "clarity_weight": 0.10,
    "required_source_classes": ["official_journal", "regulator"],
    "min_summary_chars": 40,
    "max_summary_chars": 800,
    "min_citations": 1,
}


async def _seed_default_card_definitions(conn: aiosqlite.Connection) -> None:
    for card in _V1_LAUNCH_CARDS:
        await repository.insert_card_definition(
            conn,
            id=card["id"],
            display_name=card["display_name"],
            jurisdiction=card["jurisdiction"],
            topic=card["topic"],
            description=f"{card['display_name']} regulatory monitoring card.",
            eval_spec_md=(
                f"# {card['display_name']} eval spec\n\n"
                "Default v1 stub eval spec. Real card definitions are "
                "populated from the cathedral-eval-spec content repo.\n"
            ),
            source_pool=[
                {
                    "url": "https://example.invalid/source",
                    "class": "regulator",
                    "name": "stub source",
                }
            ],
            task_templates=[
                f"Summarize material {card['display_name']} developments in the last 24 hours."
            ],
            scoring_rubric=dict(_DEFAULT_RUBRIC),
            refresh_cadence_hours=24,
            status="active",
        )


# Module-level ctx pointer used by `run_once()` test entry point. Set by
# `build_app` when the FastAPI app's lifespan starts; cleared on shutdown.
_LATEST_CTX: PublisherContext | None = None


def build_app(database_path: str = "data/publisher.db") -> FastAPI:
    """Test-friendly publisher app builder.

    Differences from `from_settings`:
    - Seeds v1 launch card definitions on startup (so contract tests
      against `eu-ai-act` etc. find a card definition without external
      seeding).
    - Uses `StubHippiusClient` (in-memory) when Hippius env vars are
      missing - the eval pipeline still round-trips bundles correctly.
    - Auto-generates an Ed25519 signing key when
      `CATHEDRAL_EVAL_SIGNING_KEY` is unset (the publisher tests don't
      need a stable key; the validator pull-loop test brings its own).
    - Auto-generates a 32-byte master KEK when CATHEDRAL_KEK_HEX /
      CATHEDRAL_MASTER_ENCRYPTION_KEY are unset.
    - Wires `StubPolarisRunner` whenever CATHEDRAL_EVAL_MODE starts with
      "stub" (the default in tests).
    - Does NOT auto-start the eval loop background task - tests drive
      ticks via `cathedral.eval.orchestrator.run_once()`. Production
      deploys use `from_settings` which starts the loop.
    """

    async def _factory() -> PublisherContext:
        # Master KEK - encryption depends on this; generate ephemeral if missing.
        if not (
            os.environ.get("CATHEDRAL_KEK_HEX") or os.environ.get("CATHEDRAL_MASTER_ENCRYPTION_KEY")
        ):
            os.environ["CATHEDRAL_KEK_HEX"] = secrets.token_bytes(32).hex()

        conn = await connect(database_path)

        # Hippius - try real config from env; fall back to stub.
        hippius: Any
        try:
            hippius = HippiusClient(HippiusConfig.from_env())
        except Exception:
            hippius = StubHippiusClient()

        # Signing key - generate if missing.
        signing_hex = os.environ.get("CATHEDRAL_EVAL_SIGNING_KEY")
        if not signing_hex:
            signing_hex = secrets.token_bytes(32).hex()
            os.environ["CATHEDRAL_EVAL_SIGNING_KEY"] = signing_hex
        signer = EvalSigner.from_env_hex(signing_hex)

        # Polaris runner - stub mode unless explicitly configured.
        eval_mode = os.environ.get("CATHEDRAL_EVAL_MODE", "stub").lower()
        polaris: PolarisRunner
        if eval_mode.startswith("stub"):
            polaris = _build_stub_polaris(eval_mode)
        elif eval_mode == "bundle":
            polaris = BundleCardRunner()
        else:
            polaris = HttpPolarisRunner(
                HttpPolarisRunnerConfig(
                    base_url=os.environ.get("POLARIS_BASE_URL", "https://api.polaris.computer"),
                    api_token=os.environ.get("POLARIS_API_TOKEN", ""),
                )
            )

        await _seed_default_card_definitions(conn)

        ctx = PublisherContext(
            db=conn,
            hippius=hippius,
            polaris=polaris,
            signer=signer,
            registry=CardRegistry.baseline(),
        )
        global _LATEST_CTX
        _LATEST_CTX = ctx
        return ctx

    return build_publisher_app(_factory, start_eval_loop=False)


def _build_stub_polaris(mode: str) -> PolarisRunner:
    """Pick a Polaris stub flavor from CATHEDRAL_EVAL_MODE.

    - "stub"                        : default happy-path stub
    - "stub-fail-polaris"           : always raises PolarisRunnerError
    - "stub-bad-card"               : returns malformed Card JSON
    - "stub-deterministic-score"    : returns valid card; score driven by
                                       CATHEDRAL_STUB_SCORE env var
    """
    from cathedral.eval.polaris_runner import (
        FailingStubPolarisRunner,
        MalformedStubPolarisRunner,
    )

    if mode == "stub-fail-polaris":
        return FailingStubPolarisRunner()
    if mode == "stub-bad-card":
        return MalformedStubPolarisRunner()
    return StubPolarisRunner()


def latest_ctx() -> PublisherContext | None:
    """Return the most recently built `PublisherContext` (set by `build_app`).

    Used by the test-friendly `cathedral.eval.orchestrator.run_once()`
    helper to find the live db / hippius / polaris / signer wiring.
    """
    return _LATEST_CTX


# Aliases the test fixture probes for.
app = build_app  # callable, returns FastAPI
