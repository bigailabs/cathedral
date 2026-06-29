from __future__ import annotations

import gzip
import hashlib
from email.message import Message

from scaffold.publisher import external_cnf_provider, refill
from scaffold.publisher.store import Store


CNF = "p cnf 2 2\n1 2 0\n-1 2 0\n"


class _Response:
    def __init__(self, *, headers: dict[str, str], body: bytes = b"", status: int = 200):
        msg = Message()
        for key, value in headers.items():
            msg[key] = value
        self.headers = msg
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._body


def _provider_env(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_CNF_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_CNF_BASE_URL", "https://provider.invalid")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_CNF_PROVIDER_ID", "qa")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_CNF_TIER", "7")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_CNF_TARGET_ACTIVE", "1")


def _meta(*, iter_id: str = "1", cnf_text: str = CNF):
    return external_cnf_provider.ProviderCnf(
        provider_id="qa",
        iter_id=iter_id,
        cnf_sha256=hashlib.sha256(cnf_text.encode("utf-8")).hexdigest(),
        num_vars=2,
        num_clauses=2,
        etag=f'"{iter_id}"',
        last_modified="Mon, 29 Jun 2026 00:00:00 GMT",
        content_length=len(cnf_text.encode("utf-8")),
        content_encoding="identity",
        accept_ranges="bytes",
    )


def test_fetch_metadata_indexes_provider_protocol(monkeypatch):
    _provider_env(monkeypatch)

    def opener(req, timeout):
        assert req.get_method() == "HEAD"
        assert timeout >= 1
        return _Response(headers={
            "X-Bitwuzla-Iter": "42",
            "X-Bitwuzla-CNF-SHA256": hashlib.sha256(CNF.encode("utf-8")).hexdigest(),
            "X-Bitwuzla-Num-Vars": "2",
            "X-Bitwuzla-Num-Clauses": "2",
            "ETag": '"42"',
            "Last-Modified": "Mon, 29 Jun 2026 00:00:00 GMT",
            "Content-Length": str(len(CNF.encode("utf-8"))),
            "Accept-Ranges": "bytes",
        })

    meta = external_cnf_provider.fetch_metadata(opener=opener)

    assert meta.iter_id == "42"
    assert meta.num_vars == 2
    assert meta.num_clauses == 2
    assert meta.accept_ranges == "bytes"
    assert meta.challenge_id.startswith("sat-t7-external-qa-42-")


def test_download_cnf_accepts_gzip_and_verifies_sha(monkeypatch):
    _provider_env(monkeypatch)
    meta = _meta()
    body = gzip.compress(CNF.encode("utf-8"))

    def urlopen(req, timeout):
        assert "if-none-match" in {key.lower() for key, _value in req.header_items()}
        return _Response(headers={"Content-Encoding": "gzip"}, body=body)

    monkeypatch.setattr(external_cnf_provider.urllib.request, "urlopen", urlopen)

    assert external_cnf_provider.download_cnf(meta) == CNF


def test_refill_mints_active_external_cnf_and_retires_stale_iter(tmp_path, monkeypatch):
    _provider_env(monkeypatch)
    store = Store(str(tmp_path / "pub.sqlite"))
    meta1 = _meta(iter_id="1")
    meta2 = _meta(iter_id="2")
    active = {"meta": meta1}

    monkeypatch.setattr(external_cnf_provider, "active_metadata", lambda: active["meta"])
    monkeypatch.setattr(external_cnf_provider, "download_cnf", lambda _meta: CNF)

    first = refill.refill_once(store, seed_input="seed")

    assert first == [{"tier": 7, "retired": 0, "minted": 1, "active": 1, "target": 1, "shape": (2, 2)}]
    rows = store.query(
        "SELECT challenge_id, status, difficulty_label, num_vars, num_clauses "
        "FROM lane_challenges ORDER BY created_at_iso, challenge_id")
    assert rows[0]["challenge_id"] == meta1.challenge_id
    assert rows[0]["status"] == "active"
    assert rows[0]["difficulty_label"].startswith("external_cnf:qa:iter=1:")
    assert rows[0]["num_vars"] == 2
    assert rows[0]["num_clauses"] == 2

    active["meta"] = meta2
    second = refill.refill_once(store, seed_input="seed")

    assert second[0]["retired"] == 1
    assert second[0]["minted"] == 1
    active_rows = store.query(
        "SELECT challenge_id FROM lane_challenges WHERE status='active'")
    retired_rows = store.query(
        "SELECT challenge_id, cnf_text FROM lane_challenges WHERE status='retired'")
    assert [row["challenge_id"] for row in active_rows] == [meta2.challenge_id]
    assert [row["challenge_id"] for row in retired_rows] == [meta1.challenge_id]
    assert retired_rows[0]["cnf_text"] == ""


def test_external_mode_waits_instead_of_falling_back_to_local_mint(tmp_path, monkeypatch):
    _provider_env(monkeypatch)
    store = Store(str(tmp_path / "pub.sqlite"))
    seen = []

    monkeypatch.setattr(external_cnf_provider, "active_metadata", lambda: None)

    out = refill.refill_once(store, seed_input="seed", log=lambda event, **kw: seen.append(event))

    assert out == [{"tier": 7, "retired": 0, "minted": 0, "active": 0, "target": 1, "shape": (6000, 25560)}]
    assert "external_cnf_waiting_for_active_metadata" in seen
    assert store.query("SELECT COUNT(*) AS n FROM lane_challenges")[0]["n"] == 0


def test_submit_solution_sets_matching_headers(monkeypatch):
    _provider_env(monkeypatch)
    meta = _meta(iter_id="3")
    seen = {}

    def urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["headers"] = dict(req.header_items())
        seen["body"] = req.data
        return _Response(headers={}, body=b"ok", status=204)

    monkeypatch.setattr(external_cnf_provider.urllib.request, "urlopen", urlopen)

    out = external_cnf_provider.submit_solution(meta, "s SATISFIABLE\nv 1 2 0\n")

    assert out == {"status": 204, "bytes": 2}
    assert seen["url"].endswith("/sol")
    assert seen["method"] == "POST"
    assert seen["headers"]["X-bitwuzla-iter"] == "3"
    assert seen["headers"]["X-bitwuzla-cnf-sha256"] == meta.cnf_sha256
    assert seen["body"].startswith(b"s SATISFIABLE")
