import pytest


@pytest.fixture(autouse=True)
def _stable_cnf_token_secret(monkeypatch):
    """Submit-role apps fail closed without a stable CNF token secret (production
    contract enforced by ``_cnf_token_secret`` / ``publisher_verify.py``: split
    submit replicas must share one secret so a CNF token minted on one validates
    on another). Provide a fixed test secret so any test that builds a
    submit-role app can construct it. A test that specifically exercises the
    unset/fail-closed path can ``monkeypatch.delenv`` it.
    """
    monkeypatch.setenv("CATHEDRAL_CNF_TOKEN_SECRET", "test-cnf-token-secret")
