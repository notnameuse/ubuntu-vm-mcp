import json
import os
from pathlib import Path

import pytest

from tests.conftest import make_plan
from tools.sync.token_store import TokenStateError, TokenStore


def test_token_is_one_time_and_claim_is_exclusive(tmp_path: Path):
    store = TokenStore(tmp_path)
    plan = make_plan()
    token = store.create(plan, "fingerprint")

    claimed = store.claim(token, plan.confirm_digest, "fingerprint")
    assert claimed["state"] == "applying"

    with pytest.raises(TokenStateError, match="stale"):
        store.claim(token, plan.confirm_digest, "fingerprint")


def test_claim_failure_keeps_token_ready(tmp_path: Path):
    store = TokenStore(tmp_path)
    plan = make_plan()
    token = store.create(plan, "fingerprint")

    with pytest.raises(TokenStateError, match="digest"):
        store.claim(token, "wrong", "fingerprint")
    assert store.load(token)["state"] == "ready"

    with pytest.raises(TokenStateError, match="config"):
        store.claim(token, plan.confirm_digest, "wrong")
    assert store.load(token)["state"] == "ready"


def test_expired_token_is_rejected(tmp_path: Path):
    now = [1000.0]
    store = TokenStore(tmp_path, token_ttl_seconds=0, clock=lambda: now[0])
    token = store.create(make_plan(), "fingerprint")
    now[0] += 1

    with pytest.raises(TokenStateError, match="expired"):
        store.load(token)


def test_corrupt_state_is_reported(tmp_path: Path):
    (tmp_path / "current.json").write_text("{", encoding="utf-8")
    store = TokenStore(tmp_path)

    result = store.load("anything")

    assert result["status"] == "state_corrupted"
    assert result["state"] == "corrupted"


def test_cancel_invalidates_token(tmp_path: Path):
    store = TokenStore(tmp_path)
    token = store.create(make_plan(), "fingerprint")

    store.cancel(token)

    with pytest.raises(TokenStateError, match="stale"):
        store.load(token)


def test_active_claim_blocks_new_preview(tmp_path: Path):
    now = [100.0]
    store = TokenStore(tmp_path, token_ttl_seconds=10, transfer_timeout_seconds=1, clock=lambda: now[0])
    plan = make_plan()
    token = store.create(plan, "fingerprint")
    store.claim(token, plan.confirm_digest, "fingerprint")

    with pytest.raises(TokenStateError, match="applying"):
        store.create(make_plan(), "fingerprint")


def test_stale_applying_preview_can_be_replaced(tmp_path: Path):
    now = [100.0]
    store = TokenStore(tmp_path, token_ttl_seconds=10, transfer_timeout_seconds=1, clock=lambda: now[0])
    plan = make_plan()
    token = store.create(plan, "fingerprint")
    store.claim(token, plan.confirm_digest, "fingerprint")
    now[0] += 31

    replacement = store.create(make_plan(), "fingerprint")

    assert replacement != token
    assert store.load(replacement)["state"] == "ready"


def test_finish_consumes_token_and_prevents_claim(tmp_path: Path):
    store = TokenStore(tmp_path)
    plan = make_plan()
    token = store.create(plan, "fingerprint")
    store.claim(token, plan.confirm_digest, "fingerprint")

    store.finish(token, {"success": True, "status": "all_applied"})

    with pytest.raises(TokenStateError, match="stale"):
        store.claim(token, plan.confirm_digest, "fingerprint")


def test_touch_updates_applying_progress(tmp_path: Path):
    now = [100.0]
    store = TokenStore(tmp_path, clock=lambda: now[0])
    plan = make_plan()
    token = store.create(plan, "fingerprint")
    store.claim(token, plan.confirm_digest, "fingerprint")
    now[0] += 5

    store.touch(token)

    state = store.load(token)
    assert state["progress_at"] == 105.0


def test_cancel_applying_requires_force(tmp_path: Path):
    store = TokenStore(tmp_path)
    plan = make_plan()
    token = store.create(plan, "fingerprint")
    store.claim(token, plan.confirm_digest, "fingerprint")

    with pytest.raises(TokenStateError, match="applying"):
        store.cancel(token)

    store.cancel(token, force=True)
    with pytest.raises(TokenStateError, match="stale"):
        store.load(token)


def test_active_claim_lock_returns_busy(tmp_path: Path):
    now = [100.0]
    store = TokenStore(tmp_path, token_ttl_seconds=10, clock=lambda: now[0])
    plan = make_plan()
    token = store.create(plan, "fingerprint")
    (tmp_path / "claim.lock").write_text(json.dumps({"pid": 999, "token": "other", "acquired_at": 95.0}))

    with pytest.raises(TokenStateError, match="busy"):
        store.claim(token, plan.confirm_digest, "fingerprint")

    assert store.load(token)["state"] == "ready"


def test_stale_claim_lock_is_recovered(tmp_path: Path):
    now = [100.0]
    store = TokenStore(tmp_path, token_ttl_seconds=10, clock=lambda: now[0])
    plan = make_plan()
    token = store.create(plan, "fingerprint")
    (tmp_path / "claim.lock").write_text(json.dumps({"pid": 999, "token": "other", "acquired_at": 80.0}))

    claimed = store.claim(token, plan.confirm_digest, "fingerprint")

    assert claimed["state"] == "applying"
    assert not (tmp_path / "claim.lock").exists()


def test_new_preview_replaces_ready_token(tmp_path: Path):
    store = TokenStore(tmp_path)
    first = store.create(make_plan(), "fingerprint")
    second = store.create(make_plan(), "fingerprint")

    assert second != first
    with pytest.raises(TokenStateError, match="stale"):
        store.load(first)
    assert store.load(second)["state"] == "ready"


def test_force_cancel_recovers_stale_applying(tmp_path: Path):
    now = [100.0]
    store = TokenStore(tmp_path, token_ttl_seconds=10, transfer_timeout_seconds=1, clock=lambda: now[0])
    plan = make_plan()
    token = store.create(plan, "fingerprint")
    store.claim(token, plan.confirm_digest, "fingerprint")
    now[0] += 31

    store.cancel(token, force=True)

    with pytest.raises(TokenStateError, match="stale"):
        store.load(token)


def test_malformed_ready_state_is_corrupted(tmp_path: Path):
    (tmp_path / "current.json").write_text(json.dumps({"token": "x", "state": "ready"}), encoding="utf-8")
    store = TokenStore(tmp_path)

    result = store.load("x")

    assert result["state"] == "corrupted"


def test_create_is_busy_when_claim_lock_is_active(tmp_path: Path):
    now = [100.0]
    store = TokenStore(tmp_path, token_ttl_seconds=10, clock=lambda: now[0])
    (tmp_path / "claim.lock").write_text(json.dumps({"pid": 999, "token": "other", "acquired_at": 95.0}))

    with pytest.raises(TokenStateError, match="busy"):
        store.create(make_plan(), "fingerprint")


def test_state_round_trip_preserves_plan_metadata(tmp_path: Path):
    plan = make_plan("a.cpp", b"a", "b.cpp", b"b", max_files=1)
    store = TokenStore(tmp_path)
    token = store.create(plan, "fingerprint")

    state = store.load(token)

    assert state["has_more"] is True
    assert state["deferred_count"] == 1
    assert state["plan_status"] == plan.status
    assert state["conflicts"] == []
    assert state["errors"] == []
    assert state["remote_excluded_count"] == 0
    assert state["window"][0]["relative_path"] == "a.cpp"
    assert state["items"]["b.cpp"]["kind"] == "add"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are required")
def test_state_file_is_private(tmp_path: Path):
    store = TokenStore(tmp_path)
    store.create(make_plan(), "fingerprint")

    mode = (tmp_path / "current.json").stat().st_mode & 0o777
    assert mode == 0o600
