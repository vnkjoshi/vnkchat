# strategies.py
import os
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Tuple, Optional
from sqlalchemy import func
from app.shoonya_integration import get_previous_ohlc, ShoonyaAPIException
from logic.trade_decision import decide_trade
from app.exceptions import OrderPendingException

# How many workers total, and which one this is (0-based)
TOTAL_SHARDS   = int(os.environ.get("TOTAL_SHARDS",   "1"))
WORKER_SHARD_ID = int(os.environ.get("WORKER_SHARD_ID", "0"))

logger = logging.getLogger(__name__)

# ---------------------------
# STEP 1: Unified Decision Function (Refactored)
# ---------------------------
def evaluate_trade_decision(api, script, live_ltp, strategy, now, threshold_cache=None):
    """
    Wrapper that ensures thresholds are fetched, then applies pure decision logic.
    """
    from flask import current_app
    from main import socketio

    threshold_cache = threshold_cache or {}
    today = now.date()

    # ── ENTRY threshold fetch ───────────────────────────────────────
    cache_key = (script.script_name, strategy.entry_basis)
    # fetch once per cycle or if stale
    if script.entry_threshold is None or script.entry_threshold_date != today:
        if cache_key in threshold_cache:
            thr = threshold_cache[cache_key]
        else:
            try:
                thr = get_previous_ohlc(api, script.script_name, strategy.entry_basis)
            except Exception as e:
                logger.warning(
                    "Failed to fetch entry threshold for %s: %s",
                    script.script_name, e,
                    extra={"user_id": getattr(script, "user_id", None),
                        "script_id": getattr(script, "id", None)}
                )
                thr = None
            threshold_cache[cache_key] = thr
        if thr is not None:
            script.entry_threshold = thr
            script.entry_threshold_date = today

    # ── Prepare re-entry config ────────────────────────────────────
    re_cfg = {}
    if current_app.config["FEATURE_FLAGS"].get("enable_reentry") \
       and strategy.reentry_params:
        re_cfg = json.loads(strategy.reentry_params)
        prev_cfg = re_cfg.get("prev_day")
        if prev_cfg:
            cache_key_re = (script.script_name, prev_cfg["basis"])
            if script.reentry_threshold is None or script.reentry_threshold_date != today:
                if cache_key_re in threshold_cache:
                    thr_re = threshold_cache[cache_key_re]
                else:
                    try:
                        thr_re = get_previous_ohlc(
                            api, script.script_name, prev_cfg["basis"]
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to fetch re-entry threshold for %s: %s",
                            script.script_name, e,
                            extra={"user_id": getattr(script, "user_id", None),
                                   "script_id": getattr(script, "id", None)}
                        )
                        thr_re = None
                    threshold_cache[cache_key_re] = thr_re

                if thr_re is not None:
                    script.reentry_threshold = thr_re
                    script.reentry_threshold_date = today

    # ── Delegate to pure decision logic ────────────────────────────
    decision = decide_trade(
        status=script.status,
        live_ltp=live_ltp,
        today=today,
        entry_threshold=script.entry_threshold,
        entry_percentage=strategy.entry_percentage,
        last_entry_date=getattr(script, 'last_entry_date', None),
        weighted_avg_price=getattr(script, 'weighted_avg_price', None),
        profit_target_type=strategy.profit_target_type,
        profit_target_value=strategy.profit_target_value,
        stop_loss_type=strategy.stop_loss_type,
        stop_loss_value=strategy.stop_loss_value,
        last_trade_date=getattr(script, 'last_trade_date', None),
        reentry_params=re_cfg,
        reentry_threshold=script.reentry_threshold,
        last_buy_price=getattr(script, 'last_buy_price', None),
    )

    return decision

# ----------------------------
# build_strategy_state
# ----------------------------
"""
Iterates over the user's strategies and scripts to form a dictionary mapping each script's identifier to its current trading 
configuration and dynamic metrics (e.g., last trade date, current LTP, thresholds).
Usage: Supplies the frontend with real-time state information.
"""
def build_strategy_state(user, existing_state=None):
    """
    Build and return a dictionary representing the user's active strategy state.
    The state dictionary maps each script's name (tsym) to its current state, including
    dynamic trading information and static configuration details.
    """
    state = {}
    # Iterate over each strategy of the user.
    for strategy in user.strategies:
        # Build a config dictionary with the static parameters defined when creating the strategy.
        config = {
            "entry_basis": strategy.entry_basis,
            "entry_percentage": strategy.entry_percentage,
            "investment_type": strategy.investment_type,
            "investment_value": strategy.investment_value,
            "profit_target_type": strategy.profit_target_type,
            "profit_target_value": strategy.profit_target_value,
            "stop_loss_type": strategy.stop_loss_type,
            "stop_loss_value": strategy.stop_loss_value,
            "execution_time": strategy.execution_time,
            "reentry_params": json.loads(strategy.reentry_params) if strategy.reentry_params else {}
        }
        for script in strategy.scripts:
            # Only include active scripts (exclude those marked as "Sold-out" or "Archived")
            if script.status not in ["Sold-out", "Archived"]:
                # For current_ltp, use the DB value if nonzero; otherwise, try to use the cached value.
                if existing_state and script.script_name in existing_state:
                    cached_ltp = existing_state[script.script_name].get("current_ltp", 0)
                else:
                    cached_ltp = 0
                current_ltp = script.ltp if script.ltp and script.ltp != 0 else cached_ltp
                state[script.script_name] = {
                    "token": script.token,
                    "threshold_price": script.entry_threshold if (script.entry_threshold is not None and script.entry_threshold > 0) else 0,
                    "weighted_avg_price": script.weighted_avg_price if script.weighted_avg_price is not None else 0,
                    "total_quantity": script.cumulative_qty if script.cumulative_qty is not None else 0,
                    "trade_count": script.trade_count if script.trade_count is not None else 0,
                    "strategy_params": json.loads(strategy.reentry_params) if strategy.reentry_params else {},
                    "position_open": True if script.status == "Running" else False,
                    "last_trade_date": script.last_trade_date.isoformat() if script.last_trade_date else None,
                    "current_ltp": current_ltp,
                    "status": script.status,
                    "configuration": config # Include the static configuration parameters here.
                }
    return state

def evaluate_trading_cycle(users, now=None, skip_reentry=False, skip_market_hours=False):
    """
    Main loop: merge state, enforce market hours, apply resets,
    filter scripts, decide actions, and enqueue orders.
    """
    # Lazy imports to avoid circular dependencies
    from main import db, redis_client, get_strategy_state_key, get_cached_api_instance, socketio
    from app.tasks import place_order_task, MARKET_OPEN, MARKET_CLOSE, MARKET_TZ
    from flask import current_app

    now = now or datetime.now(MARKET_TZ)
    # Market hours guard
    if not skip_market_hours and not (MARKET_OPEN <= now.time() <= MARKET_CLOSE):
        logger.info("Outside market hours: %s", now.time())
        return
    
    # one‐cycle cache to dedupe OHLC fetches: {(symbol, basis): threshold}
    threshold_cache: Dict[Tuple[str, str], Optional[float]] = {}

    for user in users:
        # Log shard processing info
        logger.info(
            "Processing user %s in shard %s/%s",
            user.id, 
            current_app.config.get("WORKER_SHARD_ID"),
            current_app.config.get("TOTAL_SHARDS"),
            extra={"user_id": user.id}
        )
        api = get_cached_api_instance(user)
        if not api or not user.strategies:
            continue

        # Merge live state into Redis
        state_key = get_strategy_state_key(user.id)
        existing = {}
        raw = redis_client.get(state_key)
        if raw:
            try:
                existing = json.loads(raw.decode())
            except Exception:
                existing = {}
        fresh_state = build_strategy_state(user, existing)
        redis_client.set(state_key, json.dumps(fresh_state))
        logger.info("Merged live state for user %s", user.email, extra={"user_id": user.id})

        # Iterate strategies and scripts
        for strat in user.strategies:
            # Execution time window filter
            if strat.execution_time:
                parts = strat.execution_time.strip().split(maxsplit=1)
                if len(parts) == 2:
                    when, timestr = parts
                    try:
                        cutoff = datetime.strptime(timestr, "%H:%M").time()
                        now_t = now.time()
                        if when == "after" and now_t < cutoff:
                            continue
                        if when == "before" and now_t >= cutoff:
                            continue
                    except ValueError:
                        logger.warning(
                            "Invalid exec_time for %s: %s",
                            strat.name, timestr,
                            extra={"user_id": user.id}
                        )
            for script in strat.scripts:
                with db.session.begin_nested():
                    # Daily post-exit reset
                    if script.last_trade_date and script.last_trade_date < now.date():
                        logger.info("Post-exit reset for %s", script.script_name, extra={"user_id": user.id, "script_id": script.id})
                        script.status = "Waiting"
                        script.entry_threshold = None
                        script.entry_threshold_date = None
                        script.last_entry_date = None
                        script.reentry_threshold = None
                        script.reentry_threshold_date = None
                        script.last_buy_price = None
                        script.cumulative_qty = 0
                        script.weighted_avg_price = None
                        script.trade_count = 0
                        script.last_trade_date = None
                        script.last_order_time = None
                        continue
                    # Daily re-entry reset
                    if script.status == "Running" and getattr(script, "reentry_threshold_date", None) and script.reentry_threshold_date < now.date():
                        logger.info("Re-entry reset for %s", script.script_name, extra={"user_id": user.id, "script_id": script.id})
                        script.reentry_threshold = None
                        script.reentry_threshold_date = None
                    # Skip non-active scripts
                    if script.status in ("Sold-out", "Paused", "Failed"):
                        continue
                    # Failure cooldown – pulled from config (fallback to order cooldown if not set)
                    ft = getattr(script, "failure_timestamp", None)
                    failure_cooldown = current_app.config.get(
                        "FAILURE_COOLDOWN_SECONDS",
                        current_app.config.get("ORDER_COOLDOWN_SECONDS")
                    )
                    if ft and (now.timestamp() - float(ft)) < failure_cooldown:
                        continue

                    # General cooldown – pulled from config
                    order_cooldown = current_app.config.get("ORDER_COOLDOWN_SECONDS")
                    if script.last_order_time:
                        last_time = script.last_order_time
                        # drop tzinfo on both for a clean subtraction
                        now_naive = now.replace(tzinfo=None)
                        if last_time.tzinfo is not None:
                            last_time = last_time.replace(tzinfo=None)
                        if (now_naive - last_time).total_seconds() < order_cooldown:
                            continue

                    # ── ENSURE ENTRY THRESHOLD BEFORE LTP GATING ─────────────
                    if script.status == "Waiting":
                        today = now.date()
                        cache_key = (script.script_name, strat.entry_basis)
                        if script.entry_threshold is None or script.entry_threshold_date != today:
                            if cache_key in threshold_cache:
                                thr = threshold_cache[cache_key]
                            else:
                                try:
                                    thr = get_previous_ohlc(
                                        api,
                                        script.script_name,
                                        strat.entry_basis
                                    )
                                except Exception as e:
                                    logger.warning(
                                        "Could not fetch entry threshold for %s: %s",
                                        script.script_name, str(e),
                                        extra={"user_id": user.id, "script_id": script.id}
                                    )
                                    thr = None
                                threshold_cache[cache_key] = thr
                            if thr is not None:
                                script.entry_threshold = thr
                                script.entry_threshold_date = today
                                # ── IMMEDIATELY PUSH UPDATED STATE TO REDIS & FRONTEND ──
                                # (re-use existing_state you loaded earlier)
                                new_state = build_strategy_state(user, existing)
                                redis_client.set(state_key, json.dumps(new_state))
                                socketio.emit("strategy_update", new_state, room=str(user.id))
                    # ── END ENTRY THRESHOLD FETCH ─────────────────────────

                    # Live LTP
                    live_ltp = fresh_state.get(script.script_name, {}).get("current_ltp")
                    if not live_ltp:
                        continue

                    try:
                        # 1) Get decision
                        decision = evaluate_trade_decision(
                            api,
                            script,
                            live_ltp,
                            strat,
                            now,
                            threshold_cache
                        )
                        if decision == "NONE":
                            continue

                        # 2) Compute quantity
                        if strat.investment_type.lower() == "quantity":
                            qty = int(strat.investment_value)
                        else:
                            if live_ltp > 0:
                                qty = int(strat.investment_value / live_ltp)
                            else:
                                logger.warning(
                                    "Invalid LTP for %s", script.script_name,
                                    extra={"user_id": user.id, "script_id": script.id}
                                )
                                continue

                        # 3) Build order params
                        order_params = {
                            "buy_or_sell": "B" if decision in ("BUY", "RE-ENTRY") else "S",
                            "product_type": "C",
                            "exchange": "NSE",
                            "tradingsymbol": script.script_name,
                            "quantity": qty,
                            "discloseqty": 0,
                            "price_type": "MKT",
                            "price": 0,
                            "trigger_price": None,
                            "retention": "DAY",
                            "amo": "NO",
                            "remarks": f"Auto {decision} based on {strat.entry_basis}"
                        }
                        logger.info(
                            "Enqueue %s for %s @ %s",
                            decision, script.script_name, live_ltp,
                            extra={"user_id": user.id, "script_id": script.id}
                        )

                        # 4) Update script dates
                        if decision in ("BUY", "RE-ENTRY"):
                            script.last_entry_date = now.date()
                        elif decision == "SELL":
                            script.last_trade_date = now.date()

                        # 5) Idempotency guard (atomic SET NX + expiry)
                        # acquire_order_lock(user.id, script.id)

                        # 6) Enqueue the task
                        place_order_task.delay(user.id, script.id, decision, order_params)

                        # 🕒 Record that we just enqueued an order,
                        # so that script.last_order_time can block too-frequent submissions.
                        script.last_order_time = now

                    except OrderPendingException as e:
                        # Already pending: log + notify UI, then continue
                        logger.warning(
                            "Order already pending for %s: %s",
                            script.script_name, str(e),
                            extra={"user_id": user.id, "script_id": script.id}
                        )
                        socketio.emit(
                            "strategy_error",
                            {
                                "user_id": user.id,
                                "script_id": script.id,
                                "error": str(e)
                            },
                            namespace="/api"
                        )
                        continue

                    except ShoonyaAPIException:
                        # API hiccup fetching thresholds: skip this script
                        continue

                    except Exception as e:
                        # Any other unexpected error: log + notify, then continue
                        logger.error(
                            "Unexpected error for %s: %s",
                            script.script_name, str(e),
                            extra={"user_id": user.id, "script_id": script.id}
                        )
                        socketio.emit(
                            "strategy_error",
                            {
                                "user_id": user.id,
                                "script_id": script.id,
                                "error": str(e)
                            },
                            namespace="/api"
                        )
                        continue