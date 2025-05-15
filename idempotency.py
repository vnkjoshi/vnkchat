import redis
from flask import current_app
from app.exceptions import OrderPendingException

def acquire_order_lock(user_id: int, script_id: int, client=None):
    """
    Atomically acquire a lock in Redis for this user/script.
    Raises OrderPendingException if already locked.
    """
    key = f"order_pending:{user_id}:{script_id}"

    # allow tests (fake_redis) to inject their own client
    if client is None:
        client = redis.Redis.from_url(current_app.config['REDIS_URL'])

    # expire (seconds) driven by config, default to 600
    expire = current_app.config.get("ORDER_COOLDOWN_SECONDS", 600)
    if not client.set(key, "1", nx=True, ex=expire):
        # match your test's assertion for 'pending lock exists'
        raise OrderPendingException(f"Order pending lock exists for {user_id}:{script_id}")
