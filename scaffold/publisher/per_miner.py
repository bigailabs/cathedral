"""Per-miner unique challenge generation, serving, and scoring.

FLAG: CATHEDRAL_PERMINER_ENABLED (default off).

When the flag is OFF this module is imported but every public surface
returns immediately without touching any shared state — the rest of the
publisher is byte-identical to pre-flag behaviour.

When the flag is ON:
  * Each miner hotkey gets M unique CNF instances per epoch, seeded
    deterministically from hash(hotkey || epoch || tier || seq).
  * The instances are generated on demand (no pre-minted pool) using the
    AJM hardness method already in dimacs.py, at the same calibrated tier
    sizes as the shared pool.
  * Scoring: reward = Σ difficulty_weight of correctly-solved OWN instances
    in the epoch. Mirroring another miner's answer earns zero because the
    CNF is different.
  * No validator-interface change: the signed weight vector shape is
    identical. The scoring source changes, not the wire.

Epoch:
  The epoch key is an integer derived from a time bucket (same UTC-hour
  bucketing as the shared pool's default_seed_input). This keeps instance
  sets stable within an hour and rotates every epoch. Set
  CATHEDRAL_PERMINER_EPOCH_BUCKET_HOURS to change the window (default 1).

Allotment:
  CATHEDRAL_PERMINER_ALLOTMENT_T1   (default 10000) — tier-1 instances per miner
  CATHEDRAL_PERMINER_ALLOTMENT_T2   (default 10000) — tier-2 instances per miner
  CATHEDRAL_PERMINER_MAX_PAGE_LIMIT (default 50)    — max descriptors per tier/page
  Allotment controls total epoch supply; page limit controls request-path cost.

Difficulty weights per tier (CATHEDRAL_PERMINER_WEIGHT_T1 / T2, defaults 1.0 / 2.0):
  Tier-2 pays 2× per solve to reward harder work. With AJM hardness near
  m/n≈4.26 and the same 6000-var shape as the shared pool, tier-2 instances
  should be genuinely harder and some miners will time out on them.

Shadow mode:
  When CATHEDRAL_PERMINER_SHADOW=1 (and CATHEDRAL_PERMINER_ENABLED=1) the
  publisher records per-miner solves and computes a shadow weight vector but
  does NOT serve it at /v1/validator/weights/next. This lets us compare the
  shadow distribution vs live before committing. The shadow vector is logged
  and stored in the per_miner_shadow_scores table but never signed or served.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from ..dimacs import gen_planted_3sat, verify_witness
from . import real_corpus
from .store import Store

_EPHEMERAL_SEED_SECRET = secrets.token_bytes(32)
_SEED_SECRET_ENVS = (
    "CATHEDRAL_PERMINER_SEED_SECRET",
    "CATHEDRAL_REFILL_SEED_SECRET",
    "CATHEDRAL_PUBLISHER_SEED_SECRET",
)
_PM_ID_RE = re.compile(r"^pm-t(?P<tier>\d+)-e(?P<epoch>\d+)-[0-9a-f]{24}$")

# --------------------------------------------------------------------------
# Feature flags
# --------------------------------------------------------------------------

def perminer_enabled() -> bool:
    return os.environ.get("CATHEDRAL_PERMINER_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on"}


def perminer_shadow() -> bool:
    """Shadow mode: generate + score per-miner but don't serve the vector."""
    return perminer_enabled() and (
        os.environ.get("CATHEDRAL_PERMINER_SHADOW", "").strip().lower() in {
            "1", "true", "yes", "on"})


def seed_secret_configured() -> bool:
    """True when per-miner assignments have a stable deploy-wide seed secret."""
    return any(os.environ.get(name, "").strip() for name in _SEED_SECRET_ENVS)


def require_seed_secret() -> None:
    """Fail closed when live per-miner assignment would use an ephemeral seed.

    Without a stable secret, a restart changes every `pm-*` id and makes already
    issued assignments unverifiable. Tests and disabled code paths may still use
    the process-local fallback, but enabled public endpoints must not.
    """
    if perminer_enabled() and not seed_secret_configured():
        raise RuntimeError("per_miner_seed_secret_missing")


# --------------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def epoch_bucket_hours() -> int:
    return max(1, _env_int("CATHEDRAL_PERMINER_EPOCH_BUCKET_HOURS", 1))


def allotment_for(tier: int) -> int:
    return max(1, _env_int(f"CATHEDRAL_PERMINER_ALLOTMENT_T{tier}",
                            {1: 10_000, 2: 10_000}.get(tier, 10_000)))


# Worst-case entries are roughly cache_size * allotment_for(tier).
_RECOVER_INDEX_CACHE_SIZE = max(
    1, _env_int("CATHEDRAL_PERMINER_RECOVER_INDEX_CACHE", 64)
)


def assignment_page_limit_max() -> int:
    return max(1, min(500, _env_int("CATHEDRAL_PERMINER_MAX_PAGE_LIMIT", 50)))


def assignment_page_limit(requested: int) -> int:
    return max(1, min(int(requested), assignment_page_limit_max()))


def weight_for(tier: int) -> float:
    return _env_float(f"CATHEDRAL_PERMINER_WEIGHT_T{tier}",
                      {1: 1.0, 2: 2.0}.get(tier, 1.0))


def score_target() -> float:
    """Throughput denominator for assigned scoring."""
    configured = _env_float("CATHEDRAL_PERMINER_SCORE_TARGET", 0.0)
    if configured > 0.0:
        return configured
    total = 0.0
    for tier in TIERS:
        total += allotment_for(tier) * weight_for(tier)
    return max(1.0, total)


def method_for(tier: int) -> str:
    """Planting method per tier: tier1 = 'biased' (easy participation floor so
    weaker miners can always earn something), tier2 = 'ajm' (genuinely hard
    differentiator). Env-overridable via CATHEDRAL_PERMINER_METHOD_T{tier}."""
    default = {1: "biased", 2: "ajm"}.get(tier, "ajm")
    m = os.environ.get(f"CATHEDRAL_PERMINER_METHOD_T{tier}", "").strip().lower()
    return m if m in ("biased", "ajm") else default


# Per-miner shapes: SMALL + calibrated so on-demand generation is ~ms (not the
# ~2s of the 6000-var shared pool, which would crater the event loop when
# multiplied across miners) AND tier2 AJM is hard-but-solvable (~1s) — matching
# the shared pool's shipped tier2 calibration (n=400, m=1704, ratio≈4.26).
_TIER_SHAPE: dict[int, tuple[int, int]] = {
    1: (400, 1704),
    2: (400, 1704),
}

TIERS = list(_TIER_SHAPE.keys())


def shape_for(tier: int) -> tuple[int, int]:
    base_n, base_m = _TIER_SHAPE.get(tier, (6000, 25560))
    n = _env_int(f"CATHEDRAL_PERMINER_NVARS_T{tier}", base_n)
    m = _env_int(f"CATHEDRAL_PERMINER_NCLAUSES_T{tier}", base_m)
    return n, m


# --------------------------------------------------------------------------
# Epoch key
# --------------------------------------------------------------------------

def current_epoch() -> int:
    """UTC epoch integer — monotonically increasing, bucket-width = epoch_bucket_hours."""
    hours = epoch_bucket_hours()
    ts = int(datetime.now(timezone.utc).timestamp())
    return ts // (hours * 3600)


def parse_challenge_id(challenge_id: str) -> dict[str, int] | None:
    """Parse tier/epoch from a `pm-*` id. The seq remains secret-derived."""
    m = _PM_ID_RE.match(challenge_id or "")
    if not m:
        return None
    return {"tier": int(m.group("tier")), "epoch": int(m.group("epoch"))}


def challenge_epoch(challenge_id: str) -> int | None:
    parsed = parse_challenge_id(challenge_id)
    return parsed["epoch"] if parsed else None


# --------------------------------------------------------------------------
# Deterministic seed derivation
# --------------------------------------------------------------------------

def instance_seed(hotkey: str, epoch: int, tier: int, seq: int) -> int:
    """63-bit secret-derived seed from (hotkey, epoch, tier, seq).

    The hotkey is the primary differentiator: two miners with the same epoch/tier/seq
    get completely different CNFs because their hotkeys differ. A server secret
    is mixed in so miners cannot regenerate the planted witness from public ids.
    """
    raw = f"seed:{hotkey}:{epoch}:{tier}:{seq}"
    h = hmac.new(_seed_secret_bytes(), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    return int(h[:16], 16) & 0x7FFFFFFFFFFFFFFF


def instance_id(hotkey: str, epoch: int, tier: int, seq: int) -> str:
    """Stable, opaque challenge ID for a per-miner instance."""
    raw = f"id:{hotkey}:{epoch}:{tier}:{seq}"
    h = hmac.new(_seed_secret_bytes(), raw.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    return f"pm-t{tier}-e{epoch}-{h}"


def _seed_secret_bytes() -> bytes:
    raw = ""
    for name in _SEED_SECRET_ENVS:
        raw = os.environ.get(name, "").strip()
        if raw:
            break
    if raw:
        return raw.encode("utf-8")
    if perminer_enabled():
        raise RuntimeError("per_miner_seed_secret_missing")
    return _EPHEMERAL_SEED_SECRET


def _seed_secret_fingerprint() -> str:
    return hashlib.sha256(_seed_secret_bytes()).hexdigest()


# --------------------------------------------------------------------------
# Per-miner instance set generation
# --------------------------------------------------------------------------

@lru_cache(maxsize=4096)
def _gen_cached(hotkey: str, epoch: int, tier: int, seq: int,
                method: str, n_vars: int, n_clauses: int) -> tuple[str, list[int]]:
    """Deterministic generation, memoized so repeated fetch/verify of the same
    instance doesn't regenerate. Bounded LRU keeps memory in check."""
    seed = instance_seed(hotkey, epoch, tier, seq)
    return gen_planted_3sat(seed, n_vars, n_clauses, method=method)


REAL_FRACTION_ENV = "CATHEDRAL_V2_REAL_FRACTION"


def _real_fraction() -> float:
    """Fraction (0..1) of a miner's challenges served from the REAL generator.
    Explicit CATHEDRAL_V2_REAL_FRACTION wins. If unset, a non-"planted"
    CATHEDRAL_V2_CHALLENGE_SOURCE means legacy all-real (1.0); otherwise 0.0 —
    all planted, the default UNCHANGED live behavior."""
    raw = os.environ.get(REAL_FRACTION_ENV, "").strip()
    if raw:
        try:
            return min(1.0, max(0.0, float(raw)))
        except ValueError:
            pass
    return 1.0 if real_corpus.challenge_source() != "planted" else 0.0


def _real_pick(hotkey: str, epoch: int, tier: int, seq: int) -> float:
    """Deterministic ~uniform value in [0,1) for the real/planted decision — a
    pure function of (hotkey, epoch, tier, seq) so mint and verify agree on
    whether this exact challenge is real."""
    h = hashlib.sha256(
        f"realpick:{hotkey}:{int(epoch)}:{int(tier)}:{int(seq)}".encode("utf-8")
    ).hexdigest()
    return (int(h[:8], 16) % 1_000_000) / 1_000_000.0


def uses_real_instance(hotkey: str, epoch: int, tier: int, seq: int) -> bool:
    """True iff generate_instance() serves the REAL (unplanted) source for this
    (hotkey, epoch, tier, seq), equivalently iff its planted_assignment is
    None. This is THE source-selection predicate generate_instance uses, kept
    public so callers that already hold the CNF body (e.g. a v2_cnf_store cache
    hit) can derive the kind label without generating."""
    frac = _real_fraction()
    return frac >= 1.0 or (frac > 0.0 and _real_pick(hotkey, epoch, tier, seq) < frac)


def generate_instance(hotkey: str, epoch: int, tier: int, seq: int) -> tuple[str, str, list[int] | None]:
    """Generate ONE per-miner instance. Returns (challenge_id, cnf_text, planted_assignment).

    The planted assignment is the publisher's hidden witness — never sent to
    the miner. The miner must solve the CNF independently. tier1=biased (easy
    floor), tier2=ajm (hard) per method_for(); cached so it's cheap to re-derive.

    CATHEDRAL_V2_CHALLENGE_SOURCE (default "planted") swaps the CNF source:
    unset/"planted" is this exact path, UNCHANGED. "combinatorial"/"corpus"
    (see real_corpus.py) serve REAL, unplanted instances keyed by
    (epoch, tier, seq) — planted_assignment is None because there is no
    embedded solution; dimacs.verify_witness is still the correctness gate.
    The wire challenge_id is always instance_id() (hotkey-HMAC'd), so token
    binding is identical across sources.
    """
    if uses_real_instance(hotkey, epoch, tier, seq):
        # REAL, per-miner (salt=hotkey so miners can't copy), unplanted;
        # dimacs.verify_witness is the correctness gate.
        _content_id, cnf_text = real_corpus.generate_real_instance(
            epoch, tier, seq, salt=hotkey)
        cid = instance_id(hotkey, epoch, tier, seq)
        return cid, cnf_text, None

    n_vars, n_clauses = shape_for(tier)
    cnf_text, planted = _gen_cached(hotkey, epoch, tier, seq,
                                    method_for(tier), n_vars, n_clauses)
    cid = instance_id(hotkey, epoch, tier, seq)
    return cid, cnf_text, planted


@lru_cache(maxsize=200_000)
def item_meta(hotkey: str, epoch: int, tier: int, seq: int) -> tuple[str, str, int, bool, str]:
    """Return per-item data the challenges page needs, plus the CNF body:
    (challenge_id, cnf_sha256, nvars, is_real, cnf_text).

    IMMUTABLE for a (hotkey, epoch, tier, seq), so memoized — a warm page skips
    the expensive parse_cnf + sha256 and only mints the (time-bound) token,
    removing the per-request CPU peg that starved the worker threadpool under a
    challenges flood. The caller still bakes cnf_text into v2_cnf_store (an
    idempotent INSERT OR IGNORE, so re-baking a warm item is a cheap no-op) so the
    verify worker reads the store instead of regenerating. The token is NOT cached
    (it carries expires_at/not_before and must be minted fresh).

    is_real == (planted is None) so callers derive the kind label without
    re-running the source predicate.
    """
    import hashlib
    cid, cnf_text, planted = generate_instance(hotkey, epoch, tier, seq)
    sha = hashlib.sha256(cnf_text.encode("utf-8")).hexdigest()
    from ..dimacs import parse_cnf
    nvars, _clauses = parse_cnf(cnf_text)
    return cid, sha, nvars, (planted is None), cnf_text


def miner_instance_set(
    hotkey: str,
    epoch: int,
    *,
    offset: int = 0,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return the full allotment descriptor for a miner in an epoch.

    Returns a list of dicts with keys: challenge_id, tier, seq, n_vars, n_clauses,
    difficulty_weight. The CNF body is NOT included — fetch it via
    get_miner_cnf(). This is the per-miner analogue of active-challenges.
    """
    items = []
    start = max(0, int(offset))
    for tier in TIERS:
        n_vars, n_clauses = shape_for(tier)
        allotment = allotment_for(tier)
        dw = weight_for(tier)
        stop = allotment if limit is None else min(allotment, start + max(1, int(limit)))
        for seq in range(start, stop):
            cid = instance_id(hotkey, epoch, tier, seq)
            items.append({
                "challenge_id": cid,
                "tier": tier,
                "seq": seq,
                "epoch": epoch,
                "n_vars": n_vars,
                "n_clauses": n_clauses,
                "difficulty_weight": dw,
                "kind": "random_3sat_perminer",
            })
    return items


def get_miner_cnf(hotkey: str, epoch: int, tier: int, seq: int) -> tuple[str, str] | None:
    """Return (challenge_id, cnf_text) for a specific per-miner instance, or None
    if the parameters are out of range. Generation is on-demand and deterministic.
    """
    if tier not in TIERS:
        return None
    if seq < 0 or seq >= allotment_for(tier):
        return None
    cid, cnf_text, _planted = generate_instance(hotkey, epoch, tier, seq)
    return cid, cnf_text


# --------------------------------------------------------------------------
# Submit verification
# --------------------------------------------------------------------------

def verify_miner_submission(
    hotkey: str, epoch: int, challenge_id: str, assignment: list[int]
) -> tuple[bool, str | None]:
    """Verify that `assignment` satisfies the miner's own instance.

    Reconstructs the CNF deterministically from (hotkey, epoch, challenge_id)
    and checks the assignment. Returns (ok, rejection_reason).

    ANTI-COPY: if a miner submits another miner's assignment, it will fail
    here because this CNF is different from the one the copied answer was
    generated for.

    The challenge_id must be one that appears in THIS miner's allotment for
    this epoch. If the id was generated for a different miner, the scan below
    will find no match and return challenge_id_not_in_miner_set — even if the
    assignment would satisfy a completely different CNF.
    """
    if not challenge_id.startswith("pm-"):
        return False, "not_a_perminer_challenge"

    # Scan the miner's full allotment to find the challenge — O(M × tiers),
    # small (M defaults to 5/3 per tier). This is the canonical correctness
    # check: only challenge_ids that were generated FOR this hotkey can match.
    for tier in TIERS:
        for seq in range(allotment_for(tier)):
            cid, cnf_text, _planted = generate_instance(hotkey, epoch, tier, seq)
            if cid == challenge_id:
                return verify_miner_submission_for(
                    hotkey, epoch, tier, seq, challenge_id, assignment)

    # The challenge_id was not found in this miner's allotment — either it was
    # generated for a different hotkey (copy attack) or the epoch rolled over.
    return False, "challenge_id_not_in_miner_set"


def verify_miner_submission_for(
    hotkey: str,
    epoch: int,
    tier: int,
    seq: int,
    challenge_id: str,
    assignment: list[int],
) -> tuple[bool, str | None]:
    """Verify a submission when the assignment ledger already resolved tier/seq."""
    generated_id, cnf_text, _planted = generate_instance(hotkey, epoch, tier, seq)
    if generated_id != challenge_id:
        return False, "challenge_id_not_in_miner_set"
    ok = verify_witness(cnf_text, assignment)
    if not ok:
        return False, "witness_check_failed"
    return True, None


def recover_tier_seq_for(hotkey: str, epoch: int, challenge_id: str) -> tuple[int, int] | None:
    """Find (tier, seq) for a challenge_id in the miner's allotment.
    Returns None if the challenge_id was not generated for this hotkey+epoch.
    """
    parsed = parse_challenge_id(challenge_id)
    if not parsed or parsed["epoch"] != int(epoch) or parsed["tier"] not in TIERS:
        return None
    tier = parsed["tier"]
    seq = _instance_index(_recover_index_key(hotkey, epoch, tier)).get(challenge_id)
    if seq is not None:
        return tier, seq
    return None


def resolve_tier_seq_for(
    hotkey: str,
    epoch: int,
    challenge_id: str,
    *,
    tier: int | None = None,
    seq: int | None = None,
) -> tuple[int, int] | None:
    """Resolve ownership without depending on the assignment ledger.

    If tier+seq are supplied by the client, validation is a single deterministic
    id check. Challenge-id-only clients fall back to the bounded cached index
    used by recover_tier_seq_for().
    """
    parsed = parse_challenge_id(challenge_id)
    if not parsed or parsed["epoch"] != int(epoch):
        return None
    if tier is not None or seq is not None:
        if tier is None or seq is None:
            return None
        try:
            tier_i = int(tier)
            seq_i = int(seq)
        except (TypeError, ValueError):
            return None
        if tier_i != parsed["tier"] or tier_i not in TIERS:
            return None
        if seq_i < 0 or seq_i >= allotment_for(tier_i):
            return None
        cid = instance_id(hotkey, epoch, tier_i, seq_i)
        return (tier_i, seq_i) if cid == challenge_id else None
    return recover_tier_seq_for(hotkey, epoch, challenge_id)


def _recover_index_key(hotkey: str, epoch: int, tier: int) -> tuple[str, int, int, int, str]:
    return hotkey, int(epoch), int(tier), allotment_for(tier), _seed_secret_fingerprint()


@lru_cache(maxsize=_RECOVER_INDEX_CACHE_SIZE)
def _instance_index(key: tuple[str, int, int, int, str]) -> dict[str, int]:
    """Shared read-only cid->seq cache; do not mutate the returned dict."""
    hotkey, epoch, tier, allotment, _secret_fingerprint = key
    return {
        instance_id(hotkey, epoch, tier, seq): seq
        for seq in range(allotment)
    }


def recover_seq_for(hotkey: str, epoch: int, challenge_id: str) -> int | None:
    """Find the seq number for a challenge_id. Kept for backward compatibility."""
    result = recover_tier_seq_for(hotkey, epoch, challenge_id)
    return result[1] if result else None


# --------------------------------------------------------------------------
# Per-miner scoring (shadow + live)
# --------------------------------------------------------------------------

def compute_perminer_raw_scores(store: Store, epoch: int) -> dict[str, float]:
    """Compute reward = Σ difficulty_weight of correctly-solved OWN instances
    for each hotkey in the current epoch.

    Reads from per_miner_solves table (written by record_perminer_solve).
    Returns a hotkey->score dict. Empty if no solves exist yet.
    """
    try:
        rows = store.query(
            "SELECT miner_hotkey, SUM(difficulty_weight) AS total "
            "FROM per_miner_solves WHERE epoch=? AND verified=1 "
            "GROUP BY miner_hotkey",
            (epoch,))
    except Exception:
        return {}

    scores: dict[str, float] = {}
    for r in rows:
        hk = str(r["miner_hotkey"])
        scores[hk] = round(float(r["total"] or 0.0), 6)
    return scores


def compute_perminer_scores(store: Store, epoch: int) -> dict[str, float]:
    """Target-normalized per-miner scores for dashboards and shadow logs."""
    scores = compute_perminer_raw_scores(store, epoch)
    if not scores:
        return {}

    target = score_target()
    if target <= 0:
        return {}
    return {hk: round(min(1.0, max(0.0, s / target)), 6)
            for hk, s in scores.items()}


def record_perminer_solve(
    store: Store,
    hotkey: str,
    epoch: int,
    challenge_id: str,
    tier: int,
    seq: int,
    verified: bool,
    *,
    difficulty_weight: float | None = None,
) -> bool:
    """Record a per-miner solve attempt (idempotent — one solve per hotkey+challenge).
    Returns True if newly recorded, False if already existed.

    difficulty_weight overrides the recomputed weight_for(tier) so a caller that
    already scored the solve under its own env (the V2 verifier inside
    v2_pm_env) records EXACTLY what it verified, never a legacy/default value.
    """
    def _do(conn):
        return int(record_perminer_solve_tx(
            conn, hotkey, epoch, challenge_id, tier, seq, verified,
            difficulty_weight=difficulty_weight) or 0)

    return bool(store.write(_do))


def record_perminer_solve_tx(
    conn,
    hotkey: str,
    epoch: int,
    challenge_id: str,
    tier: int,
    seq: int,
    verified: bool,
    *,
    solved_at_iso: str | None = None,
    difficulty_weight: float | None = None,
) -> bool:
    """Transaction-local per-miner solve insert."""
    if difficulty_weight is not None:
        dw = float(difficulty_weight)
    else:
        dw = weight_for(tier) if verified else 0.0
    cur = conn.execute(
        "INSERT OR IGNORE INTO per_miner_solves"
        "(challenge_id, miner_hotkey, epoch, tier, seq, difficulty_weight, verified, solved_at_iso) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (challenge_id, hotkey, epoch, tier, seq, dw, 1 if verified else 0,
         solved_at_iso or _now_iso()))
    return bool(int(cur.rowcount or 0))


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# --------------------------------------------------------------------------
# Shadow vector
# --------------------------------------------------------------------------

def compute_shadow_vector(store: Store, epoch: int) -> dict[str, float]:
    """Compute what the signed vector WOULD look like under per-miner scoring.
    Not served; logged for comparison during shadow phase.
    """
    return compute_perminer_scores(store, epoch)
