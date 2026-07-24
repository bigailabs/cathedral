"""CNF body storage abstraction (KEYSTONE TASK 3b — broadcast tier).

CNF bodies are IMMUTABLE: a challenge_id maps to a fixed DIMACS file forever.
That makes them the ideal thing to push off the database and onto cache/edge —
a miner flood for CNFs should hit a CDN, never Postgres. This module is the seam
that makes that swappable without touching the route handlers.

Backends (env CATHEDRAL_CNF_BACKEND, default `db`):
  * `db`     — today's behaviour: the CNF text lives in lane_challenges.cnf_text
               and is served by the publisher. Still cache-friendly: the route
               sets immutable/long-max-age headers so an edge in front of the
               publisher caches each (challenge_id, token-less) body and the DB
               sees at most one read per challenge per edge node.
  * `bucket` — S3-compatible object storage (Railway bucket / R2 / S3). On mint
               the CNF is `put` once at key `<prefix><challenge_id>.cnf`; serving
               is a 302 redirect to a presigned URL (short-lived, preserves the
               HMAC access-gate) so the bytes stream from the CDN/edge and never
               transit the publisher or the DB. Gated by env so it is inert until
               credentials are wired (Fred); the `db` backend remains the default.

The route keeps the existing per-request HMAC token gate (app.py `_mint_token` /
`_check_token`) in BOTH backends — `bucket` simply hands the gate's blessing off
to a presigned URL with a matching TTL. Immutability lets us cache hard.

In-process CNF body cache (reliability fix):
  CNF bodies are immutable per challenge_id — once fetched from Postgres they
  NEVER change, so a bounded in-process LRU absorbs the miner flood without
  holding a DB connection at all.  Only a cache miss goes to the pool.  The
  cache is keyed by challenge_id; the body + sha256 are stored together so the
  ETag is also served from memory.  Capacity default 512 entries (covers every
  active + recently-retired challenge with room to spare); tunable via
  CATHEDRAL_CNF_MEM_CACHE_SIZE=N (0 disables).
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from dataclasses import dataclass

# How long an immutable CNF may be cached by an edge/CDN/browser. CNFs never
# change for a given challenge_id, so this is deliberately long. The HMAC token
# (short TTL) gates the FIRST fetch; once cached at the edge under its URL the
# bytes are public-immutable for that signed URL's lifetime.
CNF_CACHE_MAX_AGE = int(os.environ.get("CATHEDRAL_CNF_CACHE_MAX_AGE", str(60 * 60 * 24 * 30)))

# In-process immutable CNF body cache.  CATHEDRAL_CNF_MEM_CACHE_SIZE=0 disables.
_CNF_MEM_CACHE_SIZE = int(os.environ.get("CATHEDRAL_CNF_MEM_CACHE_SIZE", "512"))


class _LRUCache:
    """Thread-safe bounded LRU cache backed by an OrderedDict.

    Stores (cnf_text, cnf_sha256) tuples keyed by challenge_id.  CNF bodies are
    immutable so there is no invalidation path — entries live for the process
    lifetime (or until evicted by the capacity bound).
    """

    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._data: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[str, str] | None:
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return self._data[key]

    def put(self, key: str, value: tuple[str, str]) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return  # already cached — immutable, no update needed
            self._data[key] = value
            if len(self._data) > self._maxsize:
                self._data.popitem(last=False)  # evict LRU


# Module-level singleton; disabled (maxsize=0) when the env var is zero.
_cnf_mem_cache: _LRUCache | None = _LRUCache(_CNF_MEM_CACHE_SIZE) if _CNF_MEM_CACHE_SIZE > 0 else None

# Presigned-URL lifetime for the bucket backend (seconds). Matches the order of
# the active-cnf token TTL so the access-gate window is preserved end to end.
CNF_SIGNED_URL_TTL = int(os.environ.get("CATHEDRAL_CNF_SIGNED_URL_TTL", "300"))


def cnf_cache_headers(sha256: str | None = None) -> dict[str, str]:
    """Headers that let an edge/CDN cache an immutable CNF aggressively while
    still honouring the signed-URL access gate. `public` so a shared CDN may
    cache; `immutable` so revalidation never happens; a long max-age; a strong
    ETag keyed on the content hash for cheap conditional requests."""
    h = {
        "Cache-Control": f"public, max-age={CNF_CACHE_MAX_AGE}, immutable",
        "X-Cathedral-CNF-Backend": cnf_backend_name(),
    }
    if sha256:
        h["ETag"] = f'"{sha256}"'
    return h


def cnf_backend_name() -> str:
    name = os.environ.get("CATHEDRAL_CNF_BACKEND", "db").strip().lower()
    return name if name in ("db", "bucket") else "db"


@dataclass
class CNFServeResult:
    """How the route should serve a CNF.
    * mode 'inline': return `text` with PlainTextResponse + headers.
    * mode 'redirect': return a 302 to `url` (presigned) + headers.
    * mode 'not_found': opaque 404.
    """
    mode: str
    text: str | None = None
    url: str | None = None
    headers: dict[str, str] | None = None


class CNFStore:
    """Backend-switchable CNF body store. Construct once at app build.

    The `db` backend reads cnf_text from the passed Store on demand. The `bucket`
    backend talks to an S3-compatible endpoint via boto3 (imported lazily so the
    dependency is only needed when CATHEDRAL_CNF_BACKEND=bucket)."""

    def __init__(self, store) -> None:
        self.store = store
        self.backend = cnf_backend_name()
        self._s3 = None
        self._bucket = None
        self._prefix = os.environ.get("CATHEDRAL_CNF_KEY_PREFIX", "cnf/")
        if self.backend == "bucket":
            self._init_bucket()

    # ---- bucket plumbing (lazy) -------------------------------------------
    def _init_bucket(self) -> None:
        """Wire an S3-compatible client from env. Required:
        CATHEDRAL_CNF_S3_ENDPOINT, _BUCKET, _ACCESS_KEY, _SECRET_KEY
        (Railway bucket `credentials` / R2 / S3 all expose these). If any is
        missing we FAIL CLOSED back to db so a half-configured bucket never
        silently drops CNF serving."""
        endpoint = os.environ.get("CATHEDRAL_CNF_S3_ENDPOINT")
        bucket = os.environ.get("CATHEDRAL_CNF_S3_BUCKET")
        access = os.environ.get("CATHEDRAL_CNF_S3_ACCESS_KEY")
        secret = os.environ.get("CATHEDRAL_CNF_S3_SECRET_KEY")
        region = os.environ.get("CATHEDRAL_CNF_S3_REGION", "auto")
        if not (endpoint and bucket and access and secret):
            self.backend = "db"  # fail-closed: incomplete config -> serve from db
            return
        import boto3  # lazy: only when bucket backend is actually selected
        self._s3 = boto3.client(
            "s3", endpoint_url=endpoint, aws_access_key_id=access,
            aws_secret_access_key=secret, region_name=region)
        self._bucket = bucket

    def _key(self, challenge_id: str) -> str:
        return f"{self._prefix}{challenge_id}.cnf"

    # ---- write side (called on mint) --------------------------------------
    def put(self, challenge_id: str, cnf_text: str, *, sha256: str | None = None) -> None:
        """Persist a CNF body. db backend is a no-op (the body already lives in
        lane_challenges.cnf_text via the existing INSERT). bucket backend uploads
        the immutable object once."""
        if self.backend != "bucket" or self._s3 is None:
            return
        meta = {"sha256": sha256} if sha256 else {}
        self._s3.put_object(
            Bucket=self._bucket, Key=self._key(challenge_id),
            Body=cnf_text.encode("utf-8"), ContentType="text/plain",
            CacheControl=f"public, max-age={CNF_CACHE_MAX_AGE}, immutable",
            Metadata=meta)

    # ---- generic immutable object seam (V2 content-addressed artifacts) ---
    def immutable_object_backend_ready(self) -> bool:
        """Whether the existing S3/R2-compatible backend is fully configured."""
        return self.backend == "bucket" and self._s3 is not None and self._bucket is not None

    def put_immutable_object(
        self,
        key: str,
        data: bytes,
        content_type: str,
        cache_control: str,
    ) -> None:
        """Write an exact-key immutable object through the existing bucket seam."""
        if not self.immutable_object_backend_ready():
            raise RuntimeError("cnf_bucket_backend_not_configured")
        self._s3.put_object(
            Bucket=self._bucket,
            Key=str(key),
            Body=bytes(data),
            ContentType=str(content_type),
            CacheControl=str(cache_control),
        )

    def get_immutable_object(self, key: str) -> bytes | None:
        """Read an exact-key object, returning ``None`` only for not-found."""
        if not self.immutable_object_backend_ready():
            raise RuntimeError("cnf_bucket_backend_not_configured")
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=str(key))
        except Exception as exc:
            details = getattr(exc, "response", {}) or {}
            error = details.get("Error", {}) if isinstance(details, dict) else {}
            code = str(error.get("Code") or "") if isinstance(error, dict) else ""
            status = (details.get("ResponseMetadata", {}) or {}).get("HTTPStatusCode") \
                if isinstance(details, dict) else None
            if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
                return None
            raise
        return bytes(response["Body"].read())

    # ---- read side (called by the gated route, AFTER token check) ---------
    def serve(self, challenge_id: str) -> CNFServeResult:
        """Resolve how to serve a CNF the caller has already token-authorised.
        Never leaks existence: callers map not_found -> opaque 404.

        db backend: checks the in-process immutable CNF cache first.  A cache
        hit returns the body without touching Postgres at all — no connection
        held, no pool pressure.  Only a miss goes to the DB; the result is
        stored in the cache for all subsequent requests.

        bucket backend: presign path unchanged (the redirect itself is the
        edge-cache mechanism; the in-process cache is not used for that path
        since no bytes transit the publisher).
        """
        # Bucket backend: presign a short-lived GET so bytes stream from the
        # CDN, not the publisher.  Still need the sha for the ETag — one small
        # DB read for metadata only (no cnf_text column needed).
        if self.backend == "bucket" and self._s3 is not None:
            meta = self.store.query(
                "SELECT cnf_sha256 FROM lane_challenges WHERE challenge_id=?",
                (challenge_id,))
            if not meta:
                return CNFServeResult(mode="not_found")
            sha = meta[0]["cnf_sha256"]
            headers = cnf_cache_headers(sha)
            url = self._s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": self._key(challenge_id)},
                ExpiresIn=CNF_SIGNED_URL_TTL)
            return CNFServeResult(mode="redirect", url=url, headers=headers)

        # db backend — consult the in-process LRU cache first.
        if _cnf_mem_cache is not None:
            cached = _cnf_mem_cache.get(challenge_id)
            if cached is not None:
                text, sha = cached
                # Cache hit: serve from memory, no DB connection acquired.
                return CNFServeResult(mode="inline", text=text,
                                      headers=cnf_cache_headers(sha))

        # Cache miss (or cache disabled): fetch from DB, populate cache.
        rows = self.store.query(
            "SELECT cnf_text, cnf_sha256 FROM lane_challenges WHERE challenge_id=?",
            (challenge_id,))
        if not rows:
            return CNFServeResult(mode="not_found")
        sha = rows[0]["cnf_sha256"]
        text = rows[0]["cnf_text"]
        if not text:
            # metadata-only mirrored row (cnf_source='external') — no body here.
            return CNFServeResult(mode="not_found")

        # Populate cache so subsequent requests skip the DB entirely.
        if _cnf_mem_cache is not None:
            _cnf_mem_cache.put(challenge_id, (text, sha))

        headers = cnf_cache_headers(sha)
        return CNFServeResult(mode="inline", text=text, headers=headers)
