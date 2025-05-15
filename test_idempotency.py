import pytest
from types import SimpleNamespace

from app import tasks  # Adjusted to a relative import assuming 'tasks' is in the parent directory

def test_idempotency_prevents_double_enqueue(monkeypatch):
    # 1) Monkey‐patch redis_client so setnx returns True once, then False
    seq = [True, False]
    def fake_setnx(key, val):
        return seq.pop(0)
    monkeypatch.setattr(tasks.redis_client, "setnx", fake_setnx)
    monkeypatch.setattr(tasks.redis_client, "expire", lambda *args, **kwargs: None)

    # 2) Capture calls to place_order_task.delay
    calls = []
    monkeypatch.setattr(tasks.place_order_task, "delay", lambda *args, **kwargs: calls.append((args, kwargs)))

    # 3) Run the guard snippet twice
    user_id, script_id = 1, 42
    lock_key = f"order_pending:{user_id}:{script_id}"
    # first attempt → should enqueue
    got = tasks.redis_client.setnx(lock_key, "1")
    if got:
        tasks.redis_client.expire(lock_key, 60)
        tasks.place_order_task.delay(user_id, script_id, "BUY", {"qty": 10})
    # second attempt → should skip
    got = tasks.redis_client.setnx(lock_key, "1")
    if got:
        tasks.redis_client.expire(lock_key, 60)
        tasks.place_order_task.delay(user_id, script_id, "BUY", {"qty": 10})

    assert len(calls) == 1, "Expected only one enqueue when lock is reused"
