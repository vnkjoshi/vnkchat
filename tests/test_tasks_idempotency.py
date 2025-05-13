import pytest
import fakeredis

from app.tasks import place_order_task_body, OrderPendingException

@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis()

def test_place_order_idempotency(fake_redis):
    user_id, script_id = 1, 100
    lock_key = f"order_pending:{user_id}:{script_id}"

    # First call should set the key
    place_order_task_body(user_id, script_id, "BUY", {}, fake_redis)
    assert fake_redis.exists(lock_key) == 1

    # Second immediate call should raise our duplicate‐order exception
    with pytest.raises(OrderPendingException) as exc:
        place_order_task_body(user_id, script_id, "BUY", {}, fake_redis)
    assert "pending lock exists" in str(exc.value)

    # Simulate TTL expiry by deleting the lock key
    fake_redis.delete(lock_key)
    # Now it should allow a new order
    place_order_task_body(user_id, script_id, "BUY", {}, fake_redis)
