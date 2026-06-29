"""External active-CNF provider adapter.

This supports private upstreams that expose exactly one active DIMACS CNF at a
time and accept a solution for that same `(iter, sha256)` pair. Cathedral remains
the public miner-facing surface; upstream URLs are env-only and never belong in
challenge metadata returned to miners.
"""
from __future__ import annotations

from dataclasses import dataclass
import gzip
import hashlib
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable


DEFAULT_CNF_PATH = "/cnf"
DEFAULT_SOL_PATH = "/sol"
DEFAULT_TIER = 9
DEFAULT_TARGET_ACTIVE = 1

BITWUZLA_HEADERS = {
    "iter": "X-Bitwuzla-Iter",
    "sha256": "X-Bitwuzla-CNF-SHA256",
    "num_vars": "X-Bitwuzla-Num-Vars",
    "num_clauses": "X-Bitwuzla-Num-Clauses",
}


@dataclass(frozen=True)
class ProviderCnf:
    provider_id: str
    iter_id: str
    cnf_sha256: str
    num_vars: int
    num_clauses: int
    etag: str
    last_modified: str
    content_length: int
    content_encoding: str
    accept_ranges: str

    @property
    def challenge_id(self) -> str:
        digest = hashlib.sha256(
            f"{self.provider_id}:{self.iter_id}:{self.cnf_sha256}".encode("utf-8")
        ).hexdigest()[:20]
        return f"sat-t{tier()}-external-{self.provider_id}-{self.iter_id}-{digest}"

    @property
    def difficulty_label(self) -> str:
        return (
            f"external_cnf:{self.provider_id}:iter={self.iter_id}:"
            f"sha256={self.cnf_sha256[:16]}"
        )

    def public_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "iter": self.iter_id,
            "cnf_sha256": self.cnf_sha256,
            "num_vars": self.num_vars,
            "num_clauses": self.num_clauses,
            "content_length": self.content_length,
            "etag_present": bool(self.etag),
            "last_modified_present": bool(self.last_modified),
            "accept_ranges": self.accept_ranges,
        }


_cache_lock = threading.Lock()
_cache_expires_at = 0.0
_cache_meta: ProviderCnf | None = None
_cache_error: str | None = None


def enabled() -> bool:
    source = os.environ.get("CATHEDRAL_SUPPLY_SOURCE", "").strip().lower()
    if source in {"external_cnf", "active_cnf", "bitwuzla"}:
        return True
    return _env_bool("CATHEDRAL_EXTERNAL_CNF_ENABLED")


def forwarding_enabled() -> bool:
    return enabled() and _env_bool("CATHEDRAL_EXTERNAL_CNF_FORWARD_SOLUTIONS")


def provider_id() -> str:
    raw = os.environ.get("CATHEDRAL_EXTERNAL_CNF_PROVIDER_ID", "private").strip().lower()
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in {"-", "_"})
    return cleaned[:32] or "private"


def tier() -> int:
    return max(1, _env_int("CATHEDRAL_EXTERNAL_CNF_TIER", DEFAULT_TIER))


def target_active() -> int:
    return max(1, _env_int("CATHEDRAL_EXTERNAL_CNF_TARGET_ACTIVE", DEFAULT_TARGET_ACTIVE))


def status() -> dict:
    meta = active_metadata()
    return {
        "enabled": enabled(),
        "configured": configured(),
        "provider_id": provider_id(),
        "tier": tier(),
        "target_active": target_active(),
        "supports": {
            "head_metadata": True,
            "conditional_get": True,
            "identity_download": True,
            "gzip_download": True,
            "zstd_download": False,
            "byte_ranges": "advertised_not_required",
            "sat_dimacs_solution": True,
            "unsat_lrat_submission": True,
        },
        "active": meta.public_dict() if meta else None,
        "last_error": _cache_error,
    }


def configured() -> bool:
    return bool(_base_url())


def active_metadata(*, force_refresh: bool = False) -> ProviderCnf | None:
    global _cache_expires_at, _cache_meta, _cache_error
    if not enabled():
        return None
    if not configured():
        _cache_error = "missing_base_url"
        return None
    now = time.time()
    with _cache_lock:
        if not force_refresh and _cache_meta is not None and now < _cache_expires_at:
            return _cache_meta
    try:
        meta = fetch_metadata()
    except Exception as exc:
        with _cache_lock:
            _cache_error = type(exc).__name__
        return None
    with _cache_lock:
        _cache_meta = meta
        _cache_error = None
        _cache_expires_at = now + _cache_ttl()
    return meta


def fetch_metadata(*, opener: Callable | None = None) -> ProviderCnf:
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(_cnf_url(), method="HEAD", headers=_request_headers())
    with opener(req, timeout=_timeout()) as resp:
        headers = resp.headers
        return ProviderCnf(
            provider_id=provider_id(),
            iter_id=_header(headers, BITWUZLA_HEADERS["iter"], required=True),
            cnf_sha256=_header(headers, BITWUZLA_HEADERS["sha256"], required=True),
            num_vars=_int_header(headers, BITWUZLA_HEADERS["num_vars"]),
            num_clauses=_int_header(headers, BITWUZLA_HEADERS["num_clauses"]),
            etag=_header(headers, "ETag"),
            last_modified=_header(headers, "Last-Modified"),
            content_length=_int_header(headers, "Content-Length", default=0),
            content_encoding=_header(headers, "Content-Encoding") or "identity",
            accept_ranges=_header(headers, "Accept-Ranges"),
        )


def download_cnf(meta: ProviderCnf) -> str:
    headers = _request_headers()
    headers["Accept-Encoding"] = "identity, gzip"
    if meta.etag:
        headers["If-None-Match"] = meta.etag
    req = urllib.request.Request(_cnf_url(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_download_timeout()) as resp:
            body = resp.read()
            encoding = (resp.headers.get("Content-Encoding") or "identity").lower()
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            # The provider says our cached metadata is still current, but Cathedral
            # needs the body to mint a row. Retry without validators.
            req = urllib.request.Request(
                _cnf_url(),
                headers={**_request_headers(), "Accept-Encoding": "identity, gzip"},
            )
            with urllib.request.urlopen(req, timeout=_download_timeout()) as resp:
                body = resp.read()
                encoding = (resp.headers.get("Content-Encoding") or "identity").lower()
        else:
            raise
    if encoding == "gzip":
        body = gzip.decompress(body)
    elif encoding not in {"identity", ""}:
        raise RuntimeError(f"unsupported_cnf_content_encoding:{encoding}")
    actual = hashlib.sha256(body).hexdigest()
    if actual != meta.cnf_sha256:
        raise RuntimeError("external_cnf_sha256_mismatch")
    return body.decode("utf-8")


def submit_solution(meta: ProviderCnf, solution_body: str, *, result: str = "sat") -> dict:
    headers = {
        **_request_headers(),
        BITWUZLA_HEADERS["iter"]: meta.iter_id,
        BITWUZLA_HEADERS["sha256"]: meta.cnf_sha256,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if result.lower() == "unsat":
        headers["X-Bitwuzla-Result"] = "unsat"
        headers["X-Bitwuzla-Proof-Format"] = "lrat"
    body = solution_body.encode("utf-8")
    req = urllib.request.Request(_sol_url(), data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_submit_timeout()) as resp:
        return {"status": int(resp.status), "bytes": len(resp.read())}


def submit_solution_async(challenge_id: str, solution_body: str, *, solve_rank: int | None) -> None:
    if not forwarding_enabled() or solve_rank not in (None, 1):
        return
    meta = active_metadata()
    if meta is None or meta.challenge_id != challenge_id:
        return

    def _run() -> None:
        try:
            submit_solution(meta, solution_body, result="sat")
            print(
                "[external_cnf] forwarded_solution "
                f"provider={meta.provider_id} iter={meta.iter_id} sha={meta.cnf_sha256[:16]}"
            )
        except Exception as exc:
            print(
                "[external_cnf] forward_failed "
                f"provider={meta.provider_id} iter={meta.iter_id} error={type(exc).__name__}"
            )

    threading.Thread(target=_run, name="external-cnf-forward", daemon=True).start()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _base_url() -> str:
    return os.environ.get("CATHEDRAL_EXTERNAL_CNF_BASE_URL", "").strip().rstrip("/")


def _cnf_url() -> str:
    return _join_url(_base_url(), os.environ.get("CATHEDRAL_EXTERNAL_CNF_CNF_PATH", DEFAULT_CNF_PATH))


def _sol_url() -> str:
    return _join_url(_base_url(), os.environ.get("CATHEDRAL_EXTERNAL_CNF_SOL_PATH", DEFAULT_SOL_PATH))


def _join_url(base: str, path: str) -> str:
    return urllib.parse.urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _timeout() -> int:
    return max(1, _env_int("CATHEDRAL_EXTERNAL_CNF_TIMEOUT_SECONDS", 10))


def _download_timeout() -> int:
    return max(1, _env_int("CATHEDRAL_EXTERNAL_CNF_DOWNLOAD_TIMEOUT_SECONDS", 120))


def _submit_timeout() -> int:
    return max(1, _env_int("CATHEDRAL_EXTERNAL_CNF_SUBMIT_TIMEOUT_SECONDS", 30))


def _cache_ttl() -> int:
    return max(0, _env_int("CATHEDRAL_EXTERNAL_CNF_CACHE_TTL_SECONDS", 15))


def _request_headers() -> dict[str, str]:
    token = os.environ.get("CATHEDRAL_EXTERNAL_CNF_TOKEN", "").strip()
    headers = {"User-Agent": "cathedral-external-cnf/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _header(headers, name: str, *, required: bool = False) -> str:
    value = str(headers.get(name) or "").strip()
    if required and not value:
        raise RuntimeError(f"missing_provider_header:{name}")
    return value


def _int_header(headers, name: str, default: int | None = None) -> int:
    raw = str(headers.get(name) or "").strip()
    if not raw and default is not None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"invalid_provider_header:{name}") from exc
