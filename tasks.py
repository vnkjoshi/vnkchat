"""
tasks.py - Celery tasks for asynchronous operations.
Includes tasks for logging in, placing orders, searching scripts, getting quotes,
and fetching previous day's price series.
"""
import json
import logging
from datetime import datetime, date, timedelta, time
from zoneinfo import ZoneInfo
from sqlalchemy.orm import joinedload
from celery_app import celery  # Import our configured Celery instance
from celery.exceptions import MaxRetriesExceededError
from flask import current_app
from main import app, db, get_cached_api_instance, get_strategy_state_key, redis_client, socketio
from .shoonya_integration import initialize_shoonya, place_order, get_quotes, api_daily_price_series, get_previous_ohlc
from .models import User, StrategyScript, StrategyScriptArchive, StrategySet
from celery import Task
from celery.signals import worker_ready, task_failure
from app.metrics import CELERY_TASK_FAILURES, TASK_COUNTER, TASK_DURATION
from .strategies import evaluate_trading_cycle, build_strategy_state
from app.exceptions import ShoonyaAPIException, PlaceOrderRetry, OrderPendingException

# India market hours
MARKET_TZ    = ZoneInfo("Asia/Kolkata")
MARKET_OPEN  = time(9, 15)
MARKET_CLOSE = time(15, 30)

logger = logging.getLogger(__name__)

@worker_ready.connect
def warmup_sessions(sender, **kwargs):
    with app.app_context():
        try:
            users = User.query.all()
        except Exception as e:
            logger.error("❌ warmup_sessions: could not load users (DB?): %s", e)
            return
        for user in users:
            # fire off the registered login_api_task by reference
            sender.app.send_task('app.tasks.login_api_task', args=(user.id,))

@task_failure.connect
def handle_task_failure(sender=None, exception=None, **kwargs):
    # sender.name is the full task name, e.g. "tasks.place_order_task"
    CELERY_TASK_FAILURES.labels(task_name=sender.name).inc()

@task_failure.connect
def on_task_failure(sender=None, **kwargs):
    TASK_COUNTER.labels(task_name=sender.name, status="failure").inc()

TASK_COUNTER.labels(task_name=__name__, status="success").inc()

# ---------------------------
# TASKS (Unchanged tasks)
# ---------------------------
@celery.task
def login_api_task(user_id):
    with app.app_context():
        try:
            user = db.session.get(User, user_id)
            if not user:
                current_app.logger.error("❌ User not found for user_id: %s", user_id)
                return False

            api = get_cached_api_instance(user)
            if api:
                current_app.logger.info("✅ API login successful for user %s", user.email)
                return True
            else:
                current_app.logger.error("❌ API login failed for user %s", user.email)
                return False
        except Exception:
            current_app.logger.exception("❌ Error in login_api_task for user %s", user_id)
            raise
# -------------------------------------------------
# Mark scripts as Failed after retries
# -------------------------------------------------
class BaseTaskWithFailure(Task):
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """
        This runs after all retries are exhausted.
        args = (user_id, script_id, decision, order_params)
        """
        user_id, script_id = args[0], args[1]
        with app.app_context():
            # script = StrategyScript.query.get(script_id)
            script = db.session.get(StrategyScript, script_id)
            if script:
                script.status = "Failed"
                script.failure_timestamp = datetime.utcnow().timestamp()
                db.session.commit()
                # notify the UI
                socketio.emit(
                    "strategy_update",
                    { "error": f"Script {script.script_name} failed: {exc}" },
                    room=str(user_id)
                )
# -------------------------------------------------
# 1) Updated place_order_task
# -------------------------------------------------
def to_int(x, default=0):
    try: return int(x)
    except: return default

def to_float(x, default=0.0):
    try: return float(x)
    except: return default

def place_order_task_body(user_id, script_id, decision, order_params, redis_cli):
    """
    Core idempotent order‐placement logic without Celery or DB teardown.
    Raises OrderPendingException if a lock already exists.
    """
    # Idempotency guard: pass in redis_cli so tests and real code are uniform
    from app.utils.idempotency import acquire_order_lock
    acquire_order_lock(user_id, script_id, redis_cli)

# Then use this base task for place_order_task:
@celery.task(
    bind=True,
    base=BaseTaskWithFailure,
    autoretry_for=(PlaceOrderRetry,),
    retry_kwargs={'max_retries': 3, 'countdown': 15},
    retry_backoff=True,
    name="tasks.place_order_task"
)
def place_order_task(self, user_id, script_id, decision, order_params):
    """
    Places an order via Shoonya, waits for an Ok response,
    then fetches the tradebook fills and updates your DB.
    """
    from app.utils.idempotency import acquire_order_lock

    lock_key = f"order_pending:{user_id}:{script_id}"
    try:
        # ── 1) Grab the lock ─────────────────────────────
        acquire_order_lock(user_id, script_id)
        # ── 2) Place the order ──────────────────────────
        with app.app_context():
            with TASK_DURATION.labels(task_name='place_order_task').time():
                # 0) Load user & script
                user   = db.session.get(User, user_id)
                script = db.session.get(StrategyScript, script_id)
                if not user or not script:
                    logger.error(
                        "❌ place_order_task: Missing user or script (uid=%s, script=%s)", 
                        user_id, script_id,
                        extra={"user_id": user_id, "script_id": script_id}
                    )
                    return {"error": "Missing user or script"}

                # 1) Ensure valid API instance (try cache first, then fresh login)
                api = get_cached_api_instance(user) or initialize_shoonya(user)
                if not api:
                    raise PlaceOrderRetry("API login failed")
                
                # 2) Step 3: attempt to place the order
                result = place_order(api, **order_params)
                logger.info("✅ place_order response for %s: %r", script.script_name, result)

                # 3) If we got no response, force a fresh login and retry once immediately
                if result is None:
                    # mark for your UI
                    script.failure_timestamp = datetime.utcnow().timestamp()
                    db.session.commit()
                    logger.warning("⚠️ first attempt returned None—forcing fresh Shoonya login for %s", script.script_name)
                    # re-authenticate
                    api = initialize_shoonya(user)
                    if api:
                        result = place_order(api, **order_params)
                        logger.info("🔄 second attempt response for %s: %r", script.script_name, result)
                    # if still None, let Celery back off and retry per retry_kwargs
                    if result is None:
                        raise PlaceOrderRetry("No response after forced re-login")

                # 4) Un‑wrap list responses into a single dict
                if isinstance(result, list) and result:
                    resp = result[0]
                else:
                    resp = result or {}

                # 5) Broker rejection → mark Failed and stop retries
                if resp.get("stat") == "Not_Ok":
                    script.status            = "Failed"
                    script.failure_timestamp = datetime.utcnow().timestamp()
                    db.session.commit()
                    logger.error(
                        "❌ Order rejected by broker for %s: %s", 
                        script.script_name, 
                        resp.get("emsg") or resp,
                        extra={"user_id": user_id, "script_id": script_id}
                    )
                    # a) Let the front-end show the rejection toast
                    socketio.emit(
                        "order_update",
                        {"user_id": user_id, "script": script.script_name,
                        "error": f"Broker error: {resp.get('emsg')}"}, 
                        room=str(user_id)
                    )
                    # b) Broadcast the new full strategy state (so status badge updates)
                    # refreshed = User.query.get(user_id)
                    refreshed = db.session.get(User, user_id)
                    state     = build_strategy_state(refreshed)
                    redis_client.set(get_strategy_state_key(user_id), json.dumps(state))
                    socketio.emit("strategy_update", state, room=str(user_id))
                    return resp

                # 6) Anything other than Ok at this point is unexpected → retry
                if resp.get("stat") != "Ok":
                    script.failure_timestamp = datetime.utcnow().timestamp()
                    db.session.commit()
                    logger.error(
                        "❌ place_order unexpected response for %s: %r", 
                        script.script_name, resp,
                        extra={"user_id": user_id, "script_id": script_id}
                    )
                    socketio.emit(
                        "order_update",
                        {"user_id": user_id, "script": script.script_name, "error": "Order failed"},
                        room=str(user_id)
                    )
                    raise PlaceOrderRetry("Unexpected place_order stat")

                # 7) Success! resp["stat"] == "Ok", proceed to fetch fills...
                order_no = resp.get("norenordno") or resp.get("orderno")

                # 8) Emit raw “Ok” result
                socketio.emit(
                    "order_update",
                    { "user_id": user_id, "script": script.script_name, "result": result },
                    room=str(user_id)
                )

                # 9) Examine order history for fills or outright rejections
                history = api.single_order_history(orderno=order_no) or []
                logger.info("📖 Raw single_order_history for %s: %r", script.script_name, history)

                # 9a) If any record is a rejection, mark Failed and abort.
                for record in history:
                    if record.get("status") == "REJECTED" or record.get("st_intrn") == "REJECTED":
                        now = datetime.utcnow()
                        script.status            = "Failed"
                        script.failure_timestamp = now.timestamp()
                        # clear any phantom position
                        script.cumulative_qty     = 0
                        script.weighted_avg_price = None
                        db.session.commit()
                        # 1) Toast
                        socketio.emit(
                            "order_update",
                            {"user_id": user_id, "script": script.script_name,
                            "error": f"Order rejected: {record.get('rejreason')}"},
                            room=str(user_id)
                        )
                        # 2) Badge update
                        # refreshed = User.query.get(user_id)
                        refreshed = db.session.get(User, user_id)
                        state     = build_strategy_state(refreshed)
                        redis_client.set(get_strategy_state_key(user_id), json.dumps(state))
                        socketio.emit("strategy_update", state, room=str(user_id))
                        return result    # stop here

                # 9b) Otherwise, find the first fill record
                fill_record = None
                for record in history:
                    filled = to_int(record.get("fillshares"))
                    if filled > 0:
                        fill_record = record
                        break

                # 9c) If there was no fill at all (order still pending), skip updating the script
                if not fill_record:
                    logger.info("⏸️ No fills yet for %s (order %s); skipping state update", script.script_name, order_no, extra={"user_id": user_id, "script_id": script_id})
                    return result

                # 9d) We have a fill—compute qty & price
                filled_qty     = to_int(fill_record.get("fillshares"))
                avg_fill_price = to_float(
                    fill_record.get("avgprc"),
                    to_float(fill_record.get("prc"),
                            to_float(fill_record.get("rprc"),
                                    script.ltp or 0.0))
                )

                # 10) Update script state for BUY/RE‑ENTRY or SELL
                if decision in ("BUY", "RE-ENTRY"):
                    prev_qty = script.cumulative_qty or 0
                    new_qty  = prev_qty + filled_qty
                    prev_avg = script.weighted_avg_price or script.ltp or 0
                    script.cumulative_qty     = new_qty
                    script.weighted_avg_price = (
                        (prev_avg * prev_qty + avg_fill_price * filled_qty) / new_qty
                        if new_qty else avg_fill_price
                    )
                    script.last_buy_price  = avg_fill_price
                    script.status          = "Running"
                    script.last_entry_date = now.date()
                    script.last_order_time = now
                    script.trade_count     = (script.trade_count or 0) + 1

                else:  # SELL
                    script.status            = "Sold-out"
                    script.last_trade_date   = now.date()
                    script.entry_threshold   = None
                    script.entry_threshold_date   = None
                    script.reentry_threshold = None
                    script.reentry_threshold_date = None
                    script.last_buy_price    = None
                    logger.info("📤 [%s] EXIT complete: cleared thresholds for next cycle", script.script_name, extra={"user_id": user_id, "script_id": script_id})

                db.session.commit()

                # 11) Broadcast updated strategy state
                # refreshed = User.query.get(user_id)
                refreshed = db.session.get(User, user_id)
                state     = build_strategy_state(refreshed)
                redis_client.set(get_strategy_state_key(user_id), json.dumps(state))
                socketio.emit("strategy_update", state, room=str(user_id))

                return result

    except PlaceOrderRetry:
        # decorator will catch & retry
        raise

    except MaxRetriesExceededError:
        # final failure: mark script Failed and notify
        with app.app_context():
            # script = StrategyScript.query.get(script_id)
            script = db.session.get(StrategyScript, script_id)
            script.status            = "Failed"
            script.failure_timestamp = datetime.utcnow().timestamp()
            db.session.commit()
            socketio.emit("strategy_update", {
                "error": "Network/API error: no response after retries"
            }, room=str(user_id))
        pass
    except ShoonyaAPIException as e:
        current_app.logger.error(
            "❌ Shoonya API error in place_order_task (user=%s, script=%s): %s",
            user_id, script_id, e
        )
        # you can optionally mark script Failed here
    except Exception:
        # any other bug: log & bubble up so Celery alerts you
        current_app.logger.exception(
            "❌ Unexpected error in place_order_task (user=%s, script=%s)",
            user_id, script_id
        )
        raise
    finally:
            # 🔓 Remove the pending-order lock
            try:
                redis_client.delete(lock_key)
            except Exception as e:
                logger.warning(
                    "⚠️ Could not clear order lock %s: %s", lock_key, e,
                    extra={"user_id": user_id, "script_id": script_id}
                )

@celery.task(
    bind=True,
    autoretry_for=(ShoonyaAPIException,),
    retry_kwargs={'max_retries': 2, 'countdown': 10},
    retry_backoff=True,
    name="tasks.get_daily_price_series_task"
)
def get_daily_price_series_task(self, user_id, tradingsymbol, from_date, to_date=None, exchange="NSE"):
    """
    Fetch OHLC series; retry only on ShoonyaAPIException.
    """
    with app.app_context():
        # 1) Load user (logic error if missing)
        user = db.session.get(User, user_id)
        if not user:
            raise ValueError(f"User not found: {user_id}")

        # 2) Ensure we have a valid API instance (retry on login failure)
        api = get_cached_api_instance(user)
        if not api:
            raise ShoonyaAPIException(f"get_daily_price_series: login failed for {user.email}")

        # 3) Attempt fetch
        result = api_daily_price_series(api, tradingsymbol, from_date, to_date, exchange)
        if not result or result.get("stat") != "Ok":
            logger.warning(
                "⚠️ get_daily_price_series error for %s; retrying", 
                user.email, extra={"user_id": user_id}
            )
            # force fresh login
            api = initialize_shoonya(user)
            if not api:
                raise ShoonyaAPIException(f"get_daily_price_series: fresh login failed for {user.email}")

            result = api_daily_price_series(api, tradingsymbol, from_date, to_date, exchange)
            if not result or result.get("stat") != "Ok":
                raise ShoonyaAPIException(f"get_daily_price_series: invalid response {result}")
        return result
# ---------------------------
# Helper for threshold_price fetching
# ---------------------------
@celery.task(
    bind=True,
    autoretry_for=(ShoonyaAPIException,),
    retry_kwargs={'max_retries': 6, 'countdown': 30},
    retry_backoff=True,
    name="tasks.fetch_entry_threshold_task"
)
def fetch_entry_threshold_task(self, user_id, script_id, entry_basis):
    """
    Keep retrying until we fetch a non-None threshold.
    First 6 attempts use cached API; on the 7th we fall back to a fresh Shoonya login,
    then restart the cycle indefinitely until we succeed.
    """
    with app.app_context():
        # --- load user & script ---
        # user = User.query.get(user_id)
        user = db.session.get(User, user_id)
        # script = StrategyScript.query.get(script_id)
        script = db.session.get(StrategyScript, script_id)
        if not user or not script:
            # nothing to do
            return
        
        # ── Idempotency guard: skip if already fetching this threshold ──
        lock_key = f"threshold_fetch_pending:{user_id}:{script_id}"
        locked = redis_client.set(
            lock_key,
            "1",
            nx=True,  # only set if the key does not yet exist
            ex=current_app.config.get("FAILURE_COOLDOWN_SECONDS", 60)
        )
        if not locked:
            # another worker is already fetching; skip this run
            return

        try:
            # 1) Decide which Shoonya API to use
            if self.request.retries < 6:
                # attempts 1–6: try cache first, then fresh login if cache misses
                api = get_cached_api_instance(user) or initialize_shoonya(user)
                logger.debug("Attempt #%d: using cached API for %s", self.request.retries + 1, user.email)
            else:
                # attempt #7 (and beyond if we loop): force a fresh login
                logger.info("✅ Attempt #%d: forcing fresh Shoonya login for %s",
                            self.request.retries + 1, user.email)
                api = initialize_shoonya(user)

            if not api:
                raise ShoonyaAPIException("fetch_entry_threshold: API unavailable")

            # 2) Fetch yesterday's OHLC threshold
            thresh = get_previous_ohlc(api, script.script_name, entry_basis)
            if thresh is None:
                logger.warning("⚠️ Cached session failed to fetch threshold—forcing fresh Shoonya login for %s", script.script_name)
                api = initialize_shoonya(user)
                if not api:
                    # still nothing → let Celery retry
                    raise ShoonyaAPIException(f"No threshold returned for {script.script_name}")
                thresh = get_previous_ohlc(api, script.script_name, entry_basis)
            if thresh is None:
                # still nothing → let Celery retry
                raise ShoonyaAPIException(f"No threshold returned for {script.script_name}")

            # 3) Persist threshold to DB
            script.entry_threshold      = thresh
            script.entry_threshold_date = date.today()
            db.session.commit()

            # 4) Rebuild & store the full strategy state in Redis
            # refreshed = User.query.get(user_id)
            refreshed = db.session.get(User, user_id)
            state     = build_strategy_state(refreshed)
            redis_client.set(get_strategy_state_key(user_id), json.dumps(state))

            # 5) Push update to front‑end
            socketio.emit("strategy_update", state, room=str(user_id))

            logger.info("✅ Fetched threshold %.2f for %s", thresh, script.script_name)
            return thresh

        except MaxRetriesExceededError:
            # Celery has exhausted 6 retries—now reset counter and restart the cycle
            logger.warning("⚠️ MaxRetriesExceeded for %s—restarting threshold fetch cycle", user.email)
            self.request.retries = 0
            # schedule next cycle in 30 seconds (you can adjust)
            raise self.retry(countdown=30)

# ---------------------------
# Nightly archive task
# ---------------------------
@celery.task(name="app.tasks.archive_old_scripts_task")
def archive_old_scripts_task():
    with app.app_context():
        try:
            # fetch scripts fully exited more than 30 days ago
            cutoff = date.today() - timedelta(days=30)
            old_scripts = StrategyScript.query.filter(
                StrategyScript.status == "Sold-out",
                StrategyScript.last_trade_date != None,
                StrategyScript.last_trade_date < cutoff
            ).all()

            for s in old_scripts:
                archive = StrategyScriptArchive(
                    original_id     = s.id,
                    strategy_set_id = s.strategy_set_id,
                    script_name     = s.script_name,
                    data            = {
                        "entry_threshold":     s.entry_threshold,
                        "weighted_avg_price":    s.weighted_avg_price,
                        "cumulative_qty":      s.cumulative_qty,
                        "trade_count":         s.trade_count,
                        "last_trade_date":     s.last_trade_date.isoformat()
                    }
                )
                db.session.add(archive)
                db.session.delete(s)
            db.session.commit()
        except Exception:
            current_app.logger.exception("❌ Error in archive_old_scripts_task")
            raise

# Wrap the evaluate_trading_conditions function in a Celery task.
@celery.task(name="app.tasks.evaluate_trading_conditions_task")
def evaluate_trading_conditions_task():
    # 1) Measure task duration (optional)
    with TASK_DURATION.labels(task_name='evaluate_trading_conditions_task').time():
        with app.app_context():
            try:
                # 2) Eager-load users → credentials → strategies → scripts
                users = (
                    User.query
                        .options(
                            joinedload(User.api_credential),
                            joinedload(User.strategies).joinedload(StrategySet.scripts)
                        )
                        .all()
                )

                # 3) Delegate all the per-script logic to your service layer
                evaluate_trading_cycle(
                    users,
                    skip_reentry=not app.config['FEATURE_FLAGS']['enable_reentry'],
                    skip_market_hours=app.config['DEBUG']
                )
                # Persist any threshold (and reset) updates
                db.session.commit()
            except ShoonyaAPIException as e:
                current_app.logger.error(
                    "ShoonyaAPIException in task: %s", str(e),
                    extra={"error_type": "ShoonyaAPIException"}
                )
                socketio.emit(
                    "strategy_error",
                    {"error_type": "ShoonyaAPIException", "message": str(e)},
                    namespace="/api"
                )
            except Exception as e:
                current_app.logger.exception(
                    "Unexpected error in evaluate_trading_conditions_task: %s", str(e),
                    extra={"error": str(e)}
                )
                socketio.emit(
                    "strategy_error",
                    {"error_type": "UnexpectedException", "message": str(e)},
                    namespace="/api"
                )
                raise