"""Billing/metering tests.

Every test states the real failure it catches. The ledger is cents; cost is
metered per round from OpenRouter usage.cost and accumulated on the model
backend, then reconciled after the round.
"""

from __future__ import annotations

import threading

import pytest

from api import db


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the ledger at an isolated SQLite file per test."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    return db


# ── ledger: decrement, gate, clamp ───────────────────────────────

def test_decrement_reduces_balance_by_exact_cents(temp_db):
    """Catches: deduction charging the wrong amount (off-by, wrong column)."""
    temp_db.create_key("k", "lbl", "now", credits=100)
    new = temp_db.decrement_cents("k", 30)
    assert new == 70
    assert temp_db.get_balance("k") == 70


def test_decrement_clamps_at_zero_never_negative(temp_db):
    """Catches: a costly round billing a user into negative balance."""
    temp_db.create_key("k", "lbl", "now", credits=10)
    new = temp_db.decrement_cents("k", 999)
    assert new == 0
    assert temp_db.get_balance("k") == 0


def test_has_balance_gates_on_estimate(temp_db):
    """Catches: the round-start gate letting an underfunded run begin."""
    temp_db.create_key("k", "lbl", "now", credits=9)
    assert temp_db.has_balance("k", 9) is True   # exactly enough
    assert temp_db.has_balance("k", 10) is False  # one cent short


def test_revoked_key_has_no_balance(temp_db):
    """Catches: a revoked key still being able to start rounds."""
    temp_db.create_key("k", "lbl", "now", credits=500)
    temp_db.revoke_key("k")
    assert temp_db.get_balance("k") == 0
    assert temp_db.has_balance("k", 1) is False


def test_topup_adds_cents(temp_db):
    """Catches: Stripe top-up crediting the wrong amount."""
    temp_db.create_key("k", "lbl", "now", credits=0)
    temp_db.topup_key("k", 800)
    assert temp_db.get_balance("k") == 800


# ── migration: rounds → cents, run-once ──────────────────────────

def test_legacy_rounds_convert_to_cents_once(tmp_path, monkeypatch):
    """Catches: the migration wiping balances, or re-running and inflating them."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "legacy.db")
    monkeypatch.setattr(db, "LEGACY_ROUND_TO_CENTS", 10)
    db.init_db()

    # Simulate a legacy key: 5 rounds, marked as the old 'rounds' unit.
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO api_keys (key, label, created_at, credits_remaining, credits_unit) "
            "VALUES ('legacy', 'old', 'now', 5, 'rounds')"
        )

    db.init_db()  # migration pass: 5 rounds * 10 = 50 cents
    assert db.get_balance("legacy") == 50

    db.init_db()  # idempotent: must NOT convert again
    assert db.get_balance("legacy") == 50


def test_new_keys_are_cents_not_reconverted(temp_db):
    """Catches: create_key omitting the cents marker, so the next startup
    migration multiplies a brand-new balance as if it were rounds."""
    temp_db.create_key("k", "lbl", "now", credits=800)
    temp_db.init_db()  # another startup
    assert temp_db.get_balance("k") == 800  # unchanged, not 8000


# ── cost accumulator on the model backend ────────────────────────

class _FakeModel:
    """Minimal stand-in exercising the real accumulator logic."""
    def __init__(self):
        import sim.openrouter_model as m
        # borrow the real methods onto a bare object
        self._cost_lock = threading.Lock()
        self._cost_accumulated = 0.0
        self._record_cost = m.OpenRouterModel._record_cost.__get__(self)
        self.reset_cost = m.OpenRouterModel.reset_cost.__get__(self)


def test_record_cost_sums_usage_cost():
    """Catches: usage.cost being dropped (the original bug — response discarded)."""
    fm = _FakeModel()
    fm._record_cost({"usage": {"cost": 0.0011}})
    fm._record_cost({"usage": {"cost": 0.0009}})
    assert fm._cost_accumulated == pytest.approx(0.0020)


def test_record_cost_tolerates_missing_cost():
    """Catches: an error/edge payload with no usage.cost crashing inference."""
    fm = _FakeModel()
    fm._record_cost({})                      # no usage
    fm._record_cost({"usage": {}})           # no cost
    fm._record_cost({"usage": {"cost": None}})
    assert fm._cost_accumulated == 0.0


def test_reset_cost_returns_and_zeroes():
    """Catches: reconciliation double-billing or never zeroing between rounds."""
    fm = _FakeModel()
    fm._record_cost({"usage": {"cost": 0.05}})
    assert fm.reset_cost() == pytest.approx(0.05)
    assert fm.reset_cost() == 0.0  # second read is clean


def test_accumulator_is_thread_safe_under_concurrent_calls():
    """Catches: a race dropping cost when agents call concurrently in a round."""
    fm = _FakeModel()
    n_threads, per_thread = 20, 50

    def worker():
        for _ in range(per_thread):
            fm._record_cost({"usage": {"cost": 0.001}})

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = n_threads * per_thread * 0.001
    assert fm._cost_accumulated == pytest.approx(expected)


# ── USD → cents conversion in the runner ─────────────────────────

def test_usd_to_cents_rounds_up_never_underbills():
    """Catches: truncation that lets fractional-cent spend accumulate unbilled."""
    from api.runner import _usd_to_cents
    assert _usd_to_cents(0.0011) == 1   # 0.11¢ -> 1¢ (round up)
    assert _usd_to_cents(0.04) == 4     # exact
    assert _usd_to_cents(0.041) == 5    # 4.1¢ -> 5¢
    assert _usd_to_cents(0.0) == 0
    assert _usd_to_cents(-1.0) == 0     # never negative


# ── credits/add endpoint + mint-secret gate ──────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient with an isolated DB and a known mint secret set."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "api.db")
    db.init_db()
    from fastapi.testclient import TestClient
    from api import main
    monkeypatch.setattr(main, "MINT_SECRET", "test-secret")
    return TestClient(main.app)


def test_credits_add_requires_mint_secret(client):
    """Catches: the top-up endpoint being callable without the secret —
    i.e. any user could credit themselves for free."""
    # Mint a token (also gated) using the secret.
    tok = client.post("/tokens", headers={"X-Mint-Secret": "test-secret"}).json()["token"]

    # Wrong/missing secret must be rejected.
    r = client.post("/credits/add", json={"token": tok, "cents": 500})
    assert r.status_code == 401
    r = client.post("/credits/add", json={"token": tok, "cents": 500},
                    headers={"X-Mint-Secret": "wrong"})
    assert r.status_code == 401


def test_credits_add_tops_up_balance_with_secret(client):
    """Catches: the webhook path failing to actually credit a paid token."""
    tok = client.post("/tokens", headers={"X-Mint-Secret": "test-secret"}).json()["token"]
    r = client.post("/credits/add", json={"token": tok, "cents": 800},
                    headers={"X-Mint-Secret": "test-secret"})
    assert r.status_code == 200
    assert r.json()["balance_cents"] == 800
    # Stacks on repeat top-ups.
    r = client.post("/credits/add", json={"token": tok, "cents": 200},
                    headers={"X-Mint-Secret": "test-secret"})
    assert r.json()["balance_cents"] == 1000


def test_credits_add_rejects_unknown_token(client):
    """Catches: crediting a non-existent token (typo'd webhook payload)."""
    r = client.post("/credits/add", json={"token": "agar-nope", "cents": 100},
                    headers={"X-Mint-Secret": "test-secret"})
    assert r.status_code == 404


def test_credits_add_rejects_nonpositive(client):
    """Catches: a zero/negative top-up silently passing (bad webhook data)."""
    tok = client.post("/tokens", headers={"X-Mint-Secret": "test-secret"}).json()["token"]
    r = client.post("/credits/add", json={"token": tok, "cents": 0},
                    headers={"X-Mint-Secret": "test-secret"})
    assert r.status_code == 422  # pydantic validation


def test_mint_open_when_secret_unset(tmp_path, monkeypatch):
    """Catches: the OSS default (no secret) wrongly blocking free local minting."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "open.db")
    db.init_db()
    from fastapi.testclient import TestClient
    from api import main
    monkeypatch.setattr(main, "MINT_SECRET", "")  # OSS default: open
    c = TestClient(main.app)
    r = c.post("/tokens")  # no secret header
    assert r.status_code == 201
    assert "token" in r.json()


# ─── L3 output sanitizer: None content ──────────────────────
def test_sanitize_none_content_passes_through():
    """Pro/Sonnet sometimes return content=None when the model declines.
    Without this guard the sanitizer raised TypeError mid-round and the
    runner hung waiting on the failed agent coroutine."""
    from api.security import sanitize_llm_output
    result, was = sanitize_llm_output(None)
    assert result is None
    assert was is False

def test_sanitize_non_string_content_passes_through():
    from api.security import sanitize_llm_output
    result, was = sanitize_llm_output(42)
    assert result == 42
    assert was is False


# ─── tier resolution ─────────────────────────────────────────
def test_get_tier_unknown_falls_back_to_default():
    from api.models import get_tier, DEFAULT_TIER
    t = get_tier("does-not-exist")
    assert t.name == DEFAULT_TIER
