import os

from dotenv import load_dotenv
# Only load .env in non-test runs
if os.getenv("SKIP_LOAD_DOTENV") != "1":
    load_dotenv()

from gevent import monkey
monkey.patch_all()

import sys, time, json, logging, redis, threading
from datetime import datetime, date, timedelta
from gevent.lock import Semaphore
from gevent import sleep
from typing import Dict

from flask import (
    Response, request, jsonify, flash, redirect, url_for,
    render_template, abort, current_app, g, has_request_context
)
from flask_login import (
    login_required, login_user, logout_user, current_user
)
from flask_socketio import join_room, emit
from sqlalchemy import text
from sqlalchemy.orm import joinedload
from cryptography.fernet import Fernet

from app import create_app
from app.extensions import socketio, db, bcrypt, migrate, login_manager
from app.metrics import HTTP_REQUESTS, CELERY_QUEUE_LENGTH
from app.models import (
    User, APICredential,
    StrategySet, StrategyScript, StrategyScriptArchive
)

from api_helper import ShoonyaApiPy

from app.shoonya_integration import (
    initialize_shoonya,
    search_script,
    api_daily_price_series,
    place_order,
    get_previous_ohlc
)

from app.strategies import build_strategy_state

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# create and configure the Flask app
app = create_app()

# initialize any globals that depend on app.config
FERNET_KEY = app.config['FERNET_KEY']
fernet = Fernet(FERNET_KEY.encode())
redis_client = redis.Redis.from_url(app.config['REDIS_URL'])

# ─── Fail‐fast on missing required env vars ────────────────────────────────
required_env_vars = [
    "DATABASE_URL",      # Your SQLAlchemy connection string
    "FLASK_SECRET_KEY",  # Flask session & CSRF protection
    "FERNET_KEY",        # For encrypting Shoonya API credentials
    "REDIS_URL",         # e.g. redis://redis:6379/0
]
missing = [var for var in required_env_vars if not os.getenv(var)]
if missing:
    logging.error("❌ Missing required environment variables: %s", missing)
    sys.exit(1)
# ──────────────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
# ----------------------------
# Global dictionaries to cache API instances per user
# ----------------------------
user_api_cache: Dict[int, ShoonyaApiPy] = {}
persistent_ws_connections: Dict[int, bool] = {} # Global registry for one‐and‐only‐one websocket per user
cache_lock = threading.Lock()
# ── Lock for guarding persistent_ws_connections ─────────────
ws_lock = threading.Lock()

API_INSTANCE_TIMEOUT = 1800 # Define the cache timeout in seconds (e.g., 30 minutes)

@app.after_request
def track_requests(response):
    HTTP_REQUESTS.labels(
        method=request.method,
        endpoint=request.path,
        http_status=response.status_code
    ).inc()
    return response

# On the server side, you'll need a handler for the join event. Add this to your main.py:
@socketio.on('join')
def on_join(data):
    room = data.get("room")
    if room:
        join_room(room)
        emit("status", {"msg": f"Joined room {room}"}, room=room)
        logger.info("✅ Client joined room %s", room)

# Add flag helper
def feature_enabled(name: str) -> bool:
    # Safe lookup even if FEATURE_FLAGS missing
    flags = current_app.config.get('FEATURE_FLAGS') or {}
    return flags.get(name, False)
# ----------------------------
# user_api_cache: stores the API instance for each user (keyed by user id).
# ----------------------------
def get_cached_api_instance(user):
    """
    Returns a cached ShoonyaApiPy instance if our Redis TTL is still valid
    AND we have a local instance stored; otherwise re-login and reset TTL.
    """
    now = time.time()
    redis_key = f"user:{user.id}:api_expiry"

    # 1) Check Redis TTL
    try:
        redis_expiry = float(redis_client.get(redis_key) or 0)
    except (TypeError, ValueError):
        redis_expiry = 0

    # 2) If TTL valid AND we have a local instance, reuse it
    if now < redis_expiry:
        with cache_lock:
            api = user_api_cache.get(user.id)
        if api:
            logger.info("⏱️ Reusing existing API instance for %s", user.email)
            return api

    # 3) Otherwise, (re-)initialize and cache both locally and in Redis
    logger.info("🔄 Initializing Shoonya API for %s…", user.email)
    api = initialize_shoonya(user)
    if api:
        # Cache in this process
        with cache_lock:
            user_api_cache[user.id] = api
        # Reset TTL for other processes
        redis_client.set(redis_key, now + API_INSTANCE_TIMEOUT, ex=API_INSTANCE_TIMEOUT)
        logger.info("✅ Cached API session for %s", user.email)
    else:
        logger.error("❌ Could not login Shoonya API for %s", user.email)

    return api
# ----------------------------
# Flask-Login User Loader
# ----------------------------
@login_manager.user_loader
def load_user(user_id):
    # return User.query.get(int(user_id))
    return db.session.get(User, int(user_id))

# ----------------------------
# Routes: Authentication
# ----------------------------
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        email       = request.form['email']
        raw_pw      = request.form['password']
        confirm_pw  = request.form['confirm_password']

        # 1) Check passwords match
        if raw_pw != confirm_pw:
            flash("Passwords do not match.", "danger")
            return redirect(url_for('signup'))

        # 2) Check email uniqueness
        if User.query.filter_by(email=email).first():
            flash("Email already registered.", "warning")
            return redirect(url_for('signup'))

        # 3) Create & persist user
        user = User(email=email)
        user.set_password(raw_pw)     # hashes & salts internally
        db.session.add(user)
        db.session.commit()

        flash("Signup successful! Please log in.", "success")
        return redirect(url_for('login'))

    return render_template('signup.html')

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"]
)
limiter.init_app(app)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email  = request.form['email']
        raw_pw = request.form['password']
        user   = User.query.filter_by(email=email).first()
        if user and user.check_password(raw_pw):
            login_user(user)
            flash("Logged in successfully.", "success")
             # Check and start persistent websocket connection if not active.
            start_shoonya_websocket(user)
            return redirect(url_for('dashboard'))

        flash("Invalid credentials.", "danger")
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("Logged out successfully.", "info")
    return redirect(url_for('login'))

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form['email']
        flash("Password reset link sent (placeholder).", "info")
        logger.info("✅ Password reset requested for: %s", email)
        return redirect(url_for('login'))
    return render_template('forgot_password.html')

# ----------------------------
# Routes: API Credential Settings
# ----------------------------
@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        # Retrieve fields from the form (names must match those in settings.html)
        shoonya_user_id = request.form['shoonya_user_id']
        shoonya_password = request.form['shoonya_password']
        vendor_code = request.form['shoonya_vendor_code']  # using the same name as in settings.html
        api_secret = request.form['shoonya_app_key']         # Shoonya App Key
        imei = request.form['shoonya_imei']
        totp_secret = request.form['shoonya_totp_secret']
        # Save or update API credentials in APICredential record
        if current_user.api_credential:
            cred = current_user.api_credential
            cred.shoonya_user_id = shoonya_user_id
            cred.shoonya_password = shoonya_password
            cred.vendor_code = vendor_code
            cred.api_secret = api_secret
            cred.imei = imei
            cred.totp_secret = totp_secret
        else:
            cred = APICredential(
                shoonya_user_id=shoonya_user_id,
                shoonya_password=shoonya_password,
                vendor_code=vendor_code,
                api_secret=api_secret,
                imei=imei,
                totp_secret=totp_secret,
                user=current_user
            )
            db.session.add(cred)
        db.session.commit()
        flash("API credentials saved.", "success")
        return redirect(url_for('dashboard'))
    return render_template('settings.html', credential=current_user.api_credential)
# ----------------------------
# Toggle Pause/Resume
# ----------------------------
"""
For a given strategy set, toggles the status of all associated scripts (except those already “Sold-out” or “Failed”) 
between “Waiting” and “Paused” based on the current aggregate status.
"""
@app.route('/toggle_strategy_status/<int:strategy_id>', methods=['POST'])
@login_required
def toggle_strategy_status(strategy_id):
    # Fetch the strategy set for the current user
    strategy = StrategySet.query.filter_by(id=strategy_id, user_id=current_user.id).first_or_404()

    # Determine current aggregate status using your helper (or custom logic)
    # Here we assume that if the aggregate status is "Paused", then the user wants to resume (set to "Waiting").
    # Otherwise, we pause all scripts in the set.
    current_agg_status = aggregate_strategy_status(strategy)
    # You can decide on the desired status values; for instance, if resume then set to "Waiting" (or another initial state).
    if current_agg_status.lower() == "paused":
        new_status = "Waiting"
    else:
        new_status = "Paused"

    # Update each script in the strategy, ignoring scripts which are Sold-out or Failed.
    for script in strategy.scripts:
        if script.status not in ["Sold-out", "Failed"]:
            script.status = new_status
    try:
        db.session.commit()
        flash("Strategy set '{}' updated to {}".format(strategy.name, new_status), "info")
    except Exception as e:
        db.session.rollback()
        flash("Error updating strategy set: " + str(e), "danger")

    return redirect(url_for('dashboard'))

# ----------------------------
# Heper function handle individual script status toggle for details page
# ----------------------------
"""
Allows pausing/resuming of an individual trading script by toggling its status.
"""
@limiter.exempt
@app.route('/toggle_script_status/<int:script_id>', methods=['POST'])
@login_required
def toggle_script_status(script_id):
    # Load and verify ownership
    script = (
        StrategyScript.query
        .filter_by(id=script_id)
        .join(StrategySet)
        .filter(StrategySet.user_id == current_user.id)
        .first_or_404()
    )

    # Toggle the status
    new_status = "Waiting" if script.status.lower() == "paused" else "Paused"
    script.status = new_status

    try:
        db.session.commit()
        return jsonify({"new_status": new_status})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500
# ----------------------------
# Heper function that examines the status of each script in a strategy set and then computes an aggregate status for the entire set
# ----------------------------
def aggregate_strategy_status(strategy):
    """
    Compute the overall status for a strategy set based on its scripts' statuses.
    
    Rules:
      - If any script is 'Failed' => overall status is 'Failed'
      - Else if any script is 'Running' => overall status is 'Running'
      - Else if all scripts are 'Waiting' => overall status is 'Waiting'
      - Else if all scripts are 'Paused' => overall status is 'Paused'
      - Else if all scripts are 'Sold-out' => overall status is 'Sold-out'
      - Otherwise, you can return a default such as 'Partial'
    """
    # Collect statuses from all scripts in the set.
    statuses = [script.status.lower() for script in strategy.scripts if script.status]
    if not statuses:
        return "No Scripts"
    # If any script is Failed, that's the aggregate status.
    if any(status == "failed" for status in statuses):
        return "Failed"
    # If any script is Running, then overall status is Running.
    if any(status == "running" for status in statuses):
        return "Running"
    # If every script is Waiting, return Waiting.
    if all(status == "waiting" for status in statuses):
        return "Waiting"
    # If every script is Paused, return Paused.
    if all(status == "paused" for status in statuses):
        return "Paused"
    # If every script is Sold-out, return Sold-out.
    if all(status == "sold-out" for status in statuses):
        return "Sold-out"
    # Fallback: you could return a combined status or "Partial"
    return "Partial"
# ----------------------------
# Routes: Dashboard & Strategy Set Creation
# ----------------------------
"""
Displays the user's strategies and allows creation of new strategy sets.
Processes a POST request to create a new strategy set, including collecting re-entry conditions and adding selected scripts.
"""
@app.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    if request.method == 'POST':
        # 1) Gather form data
        app.logger.info("✅ Dashboard POST data: %s", request.form)
        if len(current_user.strategies) >= 5:
            flash("Maximum strategy sets reached.", "warning")
            return redirect(url_for('dashboard'))

        name = request.form['strategy_name']
        entry_basis = request.form['entry_basis']
        try:
            entry_percentage = float(request.form.get('entry_percentage') or 0)
        except ValueError:
            flash("Entry percentage must be a number", "danger")
            return redirect(url_for('dashboard'))
        # build reentry_conditions dict…
        reentry_conditions = {}
        if request.form.get('reentry_prev_day_enabled'):
            reentry_conditions['prev_day'] = {
            'basis': request.form['reentry_prev_day_basis'],
            'percentage': float(request.form['reentry_prev_day_percentage'] or 0)
            }
        if request.form.get('reentry_last_buy_enabled'):
            reentry_conditions['last_buy'] = {
            'percentage': float(request.form['reentry_last_buy_percentage'] or 0)
            }
        if request.form.get('reentry_weighted_enabled'):
            reentry_conditions['weighted_avg'] = {
            'percentage': float(request.form['reentry_weighted_percentage'] or 0)
            }

        investment_type   = request.form['investment_type']
        try:
            investment_value = float(request.form.get('investment_value') or 0)
        except ValueError:
            flash("Investment value must be a number", "danger")
            return redirect(url_for('dashboard'))
        profit_target_type  = request.form['profit_target_type']
        try:
            profit_target_value = float(request.form.get('profit_target_value') or 0)
        except ValueError:
            flash("Profit target must be a number", "danger")
            return redirect(url_for('dashboard'))
        stop_loss_type     = request.form['stop_loss_type']
        try:
            stop_loss_value = float(request.form.get('stop_loss_value') or 0)
        except ValueError:
            flash("Stop loss must be a number", "danger")
            return redirect(url_for('dashboard'))
        execution_time     = f"{request.form['execution_time_type']} {request.form['execution_time_value']}"

        # 2) Create and save the StrategySet
        new_set = StrategySet(
            name=name,
            entry_basis=entry_basis,
            entry_percentage=entry_percentage,
            investment_type=investment_type,
            investment_value=investment_value,
            profit_target_type=profit_target_type,
            profit_target_value=profit_target_value,
            stop_loss_type=stop_loss_type,
            stop_loss_value=stop_loss_value,
            execution_time=execution_time,
            reentry_params=json.dumps(reentry_conditions),
            user=current_user
        )
        db.session.add(new_set)

        # 3) Process & save selected scripts
        selected_scripts = json.loads(request.form.get('selected_scripts', '[]'))
        app.logger.info("✅ Parsed %d selected scripts", len(selected_scripts))
        for item in selected_scripts:
            s = StrategyScript(
                script_name=item.get("tsym"),
                token=item.get("token", ""),
                strategy_set=new_set
            )
            db.session.add(s)
        db.session.commit()
        flash("Strategy set created.", "success")
        app.logger.info("✅ Strategy set created: %s", new_set.name)

        # 4) Update websocket subscriptions immediately
        update_user_subscription(current_user)

        # now dispatch threshold fetch for each new script
        from app.tasks import fetch_entry_threshold_task

        for script in new_set.scripts:
            fetch_entry_threshold_task.delay(
                current_user.id,
                script.id,
                new_set.entry_basis
            )

        return redirect(url_for('dashboard'))

    # GET: compute aggregated status and metrics
    for strategy in current_user.strategies:
        # 1) Aggregate status (unchanged)
        strategy.agg_status = aggregate_strategy_status(strategy)

        # 2) Freeze last live LTP from Redis (same as details page)
        state_key = get_strategy_state_key(current_user.id)
        state_raw = redis_client.get(state_key)
        live_state = json.loads(state_raw.decode()) if state_raw else {}
        for s in strategy.scripts:
            entry = live_state.get(s.script_name, {})
            last_ltp = entry.get("current_ltp") or 0
            if last_ltp:
                s.ltp = last_ltp

        # 3) Compute totals exactly as in details page :contentReference[oaicite:0]{index=0}&#8203;:contentReference[oaicite:1]{index=1}
        total_investment = sum(
            (s.cumulative_qty or 0) * (s.weighted_avg_price or 0)
            for s in strategy.scripts
        )
        current_value = sum(
            (s.cumulative_qty or 0) * (s.ltp or 0)
            for s in strategy.scripts
        )
        pnl = current_value - total_investment
        pnl_percent = (pnl / total_investment * 100) if total_investment else 0
        total_trades = sum(s.trade_count or 0 for s in strategy.scripts)
        deployed_scripts = len(strategy.scripts) # Count of deployed scripts

        # 4) Attach to the strategy object so Jinja can use them
        strategy.total_investment = total_investment
        strategy.current_value    = current_value
        strategy.pnl_percent      = round(pnl_percent, 2)
        strategy.total_trades     = total_trades
        strategy.deployed_scripts     = deployed_scripts

    return render_template('dashboard.html', strategies=current_user.strategies)

# ----------------------------
# check if a strategy set is "empty" according to your criteria
# ----------------------------
def is_strategy_set_empty(strategy_set):
    # Here we consider the set empty if there are no associated scripts.
    # You can refine this check if you want to filter by status.
    return len(strategy_set.scripts) == 0
# ----------------------------
# Remove from the Redis strategy state all entries corresponding to the scripts that
# ----------------------------
def remove_strategy_set_from_cache(user, strategy_set):
    """
    Remove from the Redis strategy state all entries corresponding to the scripts that
    belonged to the deleted strategy set.
    """
    state_key = get_strategy_state_key(user.id)
    state_raw = redis_client.get(state_key)
    if not state_raw:
        return
    try:
        state = json.loads(state_raw.decode())
    except Exception:
        state = {}
    for script in strategy_set.scripts:
        script_key = script.script_name  # or use another unique field if available
        if script_key in state:
            del state[script_key]
    redis_client.set(state_key, json.dumps(state))
    logger.info("✅ Removed strategy set scripts from Redis cache for user %s", user.email)
# ----------------------------
# Retry” button in Deployed‑Scripts table
# ----------------------------
@app.route("/script/<int:script_id>/retry", methods=["POST"])
@login_required
def retry_script(script_id):
    # script = StrategyScript.query.get_or_404(script_id)
    script = db.session.get(StrategyScript, script_id) or abort(404)
    if script.status != "Failed" or script.strategy_set.user_id != current_user.id:
        return jsonify(ok=False, error="Cannot retry"), 400

    # restore status
    if (script.cumulative_qty or 0) + (script.trade_count or 0) > 0:
        script.status = "Running"
    else:
        script.status = "Waiting"
    script.failure_timestamp = None
    db.session.commit()

    # broadcast the updated full state
    state = build_strategy_state(current_user)
    redis_client.set(get_strategy_state_key(current_user.id), json.dumps(state))
    socketio.emit("strategy_update", state, room=str(current_user.id))
    return jsonify(ok=True, new_status=script.status)
# ----------------------------
# Route: Strategy Set Details & Script Management
# ----------------------------
"""
Provides detailed view and management options for a given strategy set.
Allows updating common parameters, and for each individual script, supports deletion, pause/resume, or marking as “Sold-out” (exit action).
"""
@app.route('/strategy/<int:strategy_id>', methods=['GET', 'POST'])
@login_required
def strategy_details(strategy_id):
    strategy_set = StrategySet.query.filter_by(
        id=strategy_id,
        user_id=current_user.id
    ).first_or_404()

    # --- Handle any POST actions (update set or script actions) ---
    if request.method == 'POST':
        if request.form.get("update_set"):
            # Update common parameters
            strategy_set.investment_type    = request.form['investment_type']
            try:
                strategy_set.investment_value = float(request.form.get('investment_value') or 0)
            except ValueError:
                flash("Investment value must be a number", "danger")
                return redirect(url_for('strategy_details', strategy_id=strategy_id))
            
            strategy_set.profit_target_value= float(request.form.get('profit_target_value') or 0)
            try:
                strategy_set.profit_target_value = float(request.form.get('profit_target_value') or 0)
            except ValueError:
                flash("Profit target must be a number", "danger")
                return redirect(url_for('strategy_details', strategy_id=strategy_id))
            
            strategy_set.stop_loss_type     = request.form['stop_loss_type']

            try:
                strategy_set.stop_loss_value = float(request.form.get('stop_loss_value') or 0)
            except ValueError:
                flash("Stop loss must be a number", "danger")
                return redirect(url_for('strategy_details', strategy_id=strategy_id))

            execution_time_type             = request.form['execution_time_type']
            execution_time_value            = request.form['execution_time_value']
            strategy_set.execution_time     = f"{execution_time_type} {execution_time_value}"
            db.session.commit()
            flash("Strategy set parameters updated.", "success")
        else:
            # Script‐level actions: delete, pause/resume, exit
            action   = request.form.get('action')
            script_id= request.form.get('script_id')
            script   = StrategyScript.query.filter_by(
                           id=script_id,
                           strategy_set_id=strategy_set.id
                       ).first()
            if not script:
                flash("Script not found.", "danger")
            else:
                if action == 'delete':
                    if script.status in ['Waiting', 'Sold-out', 'Running', 'Failed']:
                        db.session.delete(script)
                        flash("Script deleted.", "info")
                    else:
                        flash("Cannot delete executed script.", "warning")
                elif action == 'pause_resume':
                    script.status = "Paused" if script.status != "Paused" else "Running"
                    flash("Script pause/resume toggled.", "info")
                elif action == 'exit':
                    if script.status == "Running":
                        script.status = "Sold-out"
                        flash("Script exited (sold-out).", "info")
                    else:
                        flash("Script cannot be exited in current state.", "warning")
                db.session.commit()

        # If the set is now empty, remove it entirely
        if not strategy_set.scripts:
            db.session.delete(strategy_set)
            db.session.commit()
            remove_strategy_set_from_cache(current_user, strategy_set)
            flash("Strategy set deleted because no scripts remain.", "info")
            return redirect(url_for('dashboard'))

        return redirect(url_for('strategy_details', strategy_id=strategy_id))

    # --- GET: if set became empty (e.g. via external action), delete it ---
    if not strategy_set.scripts:
        db.session.delete(strategy_set)
        db.session.commit()
        remove_strategy_set_from_cache(current_user, strategy_set)
        flash("Strategy set deleted because no scripts remain.", "info")
        return redirect(url_for('dashboard'))

    # ----------------------------------------------------------------
    # BEFORE RENDER: Freeze last live LTP from Redis (so after‑hours it stays)
    # ----------------------------------------------------------------
    state_key = get_strategy_state_key(current_user.id)
    state_raw = redis_client.get(state_key)
    if state_raw:
        try:
            live_state = json.loads(state_raw.decode())
        except Exception as e:
            logger.error("❌ Error decoding strategy state for user %s: %s",
                         current_user.email, e)
            live_state = {}
    else:
        live_state = {}

    for script in strategy_set.scripts:
        entry = live_state.get(script.script_name, {})
        last_ltp = entry.get("current_ltp") or 0
        if last_ltp:
            # overwrite the model’s ltp for display only
            script.ltp = last_ltp

    # Compute any aggregated metrics for display
    deployed_scripts = len(strategy_set.scripts) # Count of deployed scripts
    # Per‑script metrics
    investments     = [s.cumulative_qty * (s.weighted_avg_price or 0) for s in strategy_set.scripts]
    current_values  = [s.cumulative_qty * (s.ltp or 0)             for s in strategy_set.scripts]
    pnls            = [cv - inv                                  for cv, inv in zip(current_values, investments)]
    trade_counts    = [s.trade_count or 0                        for s in strategy_set.scripts]

    # Aggregates
    total_investment    = sum(investments)
    total_current_value = sum(current_values)
    total_pnl           = sum(pnls)
    overall_pnl_percent = (total_pnl / total_investment * 100) if total_investment else 0
    total_trades        = sum(trade_counts)

    # Pass them into the template
    # --- just before render_template ---
    try:
        reentry = json.loads(strategy_set.reentry_params or "{}")
    except ValueError:
        reentry = {}

    return render_template(
        'strategy_details.html',
        strategy=        strategy_set,
        reentry_params=  reentry,
        total_investment=total_investment,
        total_current_value=total_current_value,
        total_pnl=       total_pnl,
        overall_pnl_percent=overall_pnl_percent,
        total_trades=    total_trades
    )
# ----------------------------
# Add a Helper Function to Build the Strategy State
# ----------------------------
"""
Returns a unique Redis key for storing the state (dynamic trading data) of a user's strategies.
"""
def get_strategy_state_key(user_id: int) -> str:
    return f"user:{user_id}:strategy_state"
# ----------------------------
# init_strategy_state
# ----------------------------
"""
When a user is authenticated, it re-queries the latest user data from the database and rebuilds the strategy state, 
storing the updated state in Redis.
Usage: Keeps the trading state in sync across requests and facilitates real-time updates via Socket.IO.
"""
@app.before_request
def init_strategy_state():
    # ─── Skip entirely in testing mode ──────────────────────────
    if current_app.testing:
        # Optionally set an empty state for templates
        g.strategy_state = {}
        return
    # ─── Original guards & logic ────────────────────────────────
    if not has_request_context():
        return
    
    if current_user.is_authenticated:
        # Requery the user to get the latest values from the DB.
        # refreshed_user = User.query.get(current_user.id)
        refreshed_user = db.session.get(User, current_user.id)
        state_key = get_strategy_state_key(refreshed_user.id)
        # Get the existing state from Redis.
        existing_state_json = redis_client.get(state_key)
        existing_state = json.loads(existing_state_json) if existing_state_json else {}
        # Build a new state from the refreshed user object.
        new_state = build_strategy_state(refreshed_user)
        
        # Merge in dynamic values. For threshold price, use the new DB value if it is valid.
        for script_name, new_values in new_state.items():
            if script_name in existing_state:
                dynamic = existing_state[script_name]
                # Preserve current_ltp if present.
                current_ltp = dynamic.get("current_ltp", 0)
                if current_ltp not in [None, 0]:
                    new_values["current_ltp"] = current_ltp
                # For threshold, override if the DB (new) value is nonzero.
                db_threshold = new_values.get("threshold_price", 0)
                if db_threshold > 0:
                    new_values["threshold_price"] = db_threshold
                else:
                    # Otherwise, if the cached value is nonzero, use it.
                    cached_threshold = dynamic.get("threshold_price", 0)
                    if cached_threshold not in [None, 0]:
                        new_values["threshold_price"] = cached_threshold
        # Save the new state in Redis.
        redis_client.set(state_key, json.dumps(new_state))
        logger.info("✅ Initialized (merged) strategy state for user %s", refreshed_user.email)

def log_strategy_state_periodically():
    with app.app_context():
        while True:
            # 1) Update queue length once
            try:
                length = redis_client.llen('celery')
                CELERY_QUEUE_LENGTH.labels(queue_name='default').set(length)
            except Exception as e:
                current_app.logger.warning("Could not update queue length metric: %s", e)

            # 2) Then iterate users
            users = User.query.all()
            for user in users:
                state_raw = redis_client.get(get_strategy_state_key(user.id))
                if not state_raw:
                    current_app.logger.warning("No strategy state for user %s", user.email)
                    continue
                try:
                    state_data = json.loads(state_raw)
                    current_app.logger.info("🟢 Complete strategy state for %s: %s", user.email, json.dumps(state_data))
                except Exception as e:
                    current_app.logger.error("❌ Error decoding strategy state for %s: %s", user.email, e)

            socketio.sleep(60)

# ----------------------------  
# API Integration Endpoints Using Shoonya Integration
# ----------------------------
@app.route('/search_script_api', methods=['GET'])
@login_required
def search_script_api():
    search_text = request.args.get('search_text', '').strip()
    exchange    = request.args.get('exchange', 'NSE')
    logger.info("🔍 /search_script_api: %r on %s", search_text, exchange)

    # Guard against too‑short queries
    if len(search_text) < 2:
        return jsonify({"values": [], "error": None}), 200

    def attempt_search(api_instance):
        try:
            res = search_script(api_instance, search_text, exchange)
            logger.info("🔍 Shoonya response: %r", res)
            if not res or res.get("stat") != "Ok":
                raise ValueError(res.get("emsg") or f"stat={res.get('stat')}")
            return res.get("values", []), None
        except Exception as e:
            return None, str(e)

    # 1) Try with cached API
    api = get_cached_api_instance(current_user)
    values, error = attempt_search(api) if api else (None, "no API instance")

    # 2) If that failed, force a fresh login & retry once
    if error is not None:
        logger.warning("⚠️ search_script failed (%s), re-authenticating…", error)
        api = initialize_shoonya(current_user)
        values, error = attempt_search(api)

    # 3) Normalize & return
    if values:
        return jsonify({"values": values, "error": None}), 200
    else:
        return jsonify({"values": [], "error": error or "No scripts found."}), 200
# ----------------------------
# Route: Add Script to a Strategy Set
# ----------------------------
@app.route('/add_script/<int:strategy_id>', methods=['POST'])
@login_required
def add_script(strategy_id):
    strategy_set = StrategySet.query.filter_by(
        id=strategy_id, user_id=current_user.id
    ).first_or_404()

    # 1) Parse and save the new scripts
    selected_json = request.form.get('selected_scripts', '[]')
    try:
        selected = json.loads(selected_json)
    except:
        flash("Error processing selected scripts.", "danger")
        return redirect(url_for('strategy_details', strategy_id=strategy_id))

    if len(strategy_set.scripts) + len(selected) > 10:
        flash("Adding these scripts would exceed the maximum limit of 10.", "warning")
        return redirect(url_for('strategy_details', strategy_id=strategy_id))

    new_scripts = []
    for item in selected:
        s = StrategyScript(
            script_name=item.get("tsym"),
            token=item.get("token", ""),
            strategy_set=strategy_set
        )
        db.session.add(s)
        new_scripts.append(s)
    db.session.commit()
    flash("Script(s) added to strategy set.", "success")

    # 2) Immediately update your websocket subscription
    update_user_subscription(current_user)

    # now dispatch threshold fetch for each new script
    from app.tasks import fetch_entry_threshold_task

    for script in new_scripts:
        fetch_entry_threshold_task.delay(
            current_user.id,
            script.id,
            strategy_set.entry_basis
        )

    return redirect(url_for('strategy_details', strategy_id=strategy_id))
# ----------------------------
# Route: Evaluating the Entry (or Re‑entry) Condition
# ----------------------------
def evaluate_entry_condition(api, tradingsymbol, entry_basis, entry_percentage, live_ltp):
    """
    If entry_percentage is negative => we check if LTP is <= threshold (price drop).
    If entry_percentage is positive => we check if LTP is >= threshold (price rise).
    """
    prev_value = get_previous_ohlc(api, tradingsymbol, entry_basis)
    if prev_value is None:
        return False

    if entry_percentage < 0:
        threshold = prev_value * (1 - abs(entry_percentage)/100)
        return live_ltp <= threshold
    else:
        threshold = prev_value * (1 + abs(entry_percentage)/100)
        return live_ltp >= threshold
# ----------------------------
# Live Tick Callback – UPDATED VERSION
# ----------------------------
def on_data(user, message):
    """
    Updated live tick callback:
    - Extracts the token and live LTP from the tick message.
    - Retrieves the user's strategy state from Redis.
    - Iterates over each script entry and updates the 'current_ltp' field
      if the token from the message matches the token stored for that script.
    """
    # Extract token and live last traded price (LTP) from the message.
    # logger.info("on_data called with message: %s", message)
    
    token = message.get("tk") or message.get("token")
    raw_ltp  = message.get("lp")   or message.get("ltp")

    if not token:
        logger.error("Missing token in tick message: %s", message, extra={"user_id": user.id})
        return
    
    if raw_ltp is None:
        # depth‐only update (no price) – skip quietly
        logger.debug("Skipping depth‐only tick: %s", message, extra={"user_id": user.id})
        return

    try:
        live_ltp = float(raw_ltp)
    except ValueError:
        logger.warning("Could not parse LTP %r in tick: %s", raw_ltp, message, extra={"user_id": user.id})
        return
    
    token = str(token).strip()
    
    state_key = get_strategy_state_key(user.id)
    state_raw = redis_client.get(state_key)

    # 1) If Redis empty, build a fresh template from the DB
    if state_raw:
        try:
            state = json.loads(state_raw)
        except Exception as e:
            logger.error("❌ Error decoding state for %s: %s", user.email, e)
            state = build_strategy_state(user)
    else:
        logger.info("✅ Seeding strategy state cache for %s", user.email)
        state = build_strategy_state(user)

    # 2) Merge this tick into state
    updated = False
    for script_name, entry in state.items():
        if str(entry.get("token", "")).strip() == token:
            entry["current_ltp"] = float(live_ltp)
            updated = True
            logger.info("✅ Updated current_ltp for %s → %s", script_name, live_ltp)

    if updated:
        redis_client.set(state_key, json.dumps(state))
    else:
        logger.info("❌ No matching script found for token %s in user %s state", token, user.email)

     # immediately push the new state to the client
    socketio.emit("strategy_update", state, room=str(user.id))
# ----------------------------
# Route: Start the WebSocket
# ----------------------------
def start_shoonya_websocket(user):
    """
    Start a background ShoonyaApiPy websocket for the given user.
    We no longer track threads in-process. Redis + SocketIO message
    queue will deliver events to the correct client room.
    """
    logger.info("✅ Starting Shoonya websocket for user %s", user.email)

    # ── if we've already started one for this user, skip
    with ws_lock:
        if persistent_ws_connections.get(user.id):
            logger.debug(
                "Websocket already running for user %s; skipping restart",
                user.email,
                extra={"user_id": user.id}
            )
            return
        # mark it started
        persistent_ws_connections[user.id] = True

    logger.info("✅ Starting Shoonya websocket for user %s", user.email, extra={"user_id": user.id})
    
    def run_ws():
        with app.app_context():
            # Re-fetch user + strategies & scripts
            user_obj = (
                User.query
                .options(joinedload(User.strategies).joinedload(StrategySet.scripts))
                .get(user.id)
            )
            if not user_obj:
                logger.error("❌ User not found (id=%s)", user.id)
                return

            api = get_cached_api_instance(user_obj)
            if not api:
                logger.error("❌ Could not get Shoonya API for %s", user.email)
                return

            # Order updates → emit to user's room
            def order_update_callback(msg):
                socketio.emit("order_update", msg, room=f"user_{user.id}")
                logger.info("→ order_update for %s: %s", user.email, msg)

            # Tick updates → update DB, then emit full strategy state
            def subscribe_callback(msg):
                on_data(user_obj, msg)
                # logger.info("✅ Tick message received for user %s: %s", user_obj.email, msg)
                state_key = get_strategy_state_key(user_obj.id)
                raw = redis_client.get(state_key)
                if raw:
                    try:
                        state = json.loads(raw)
                        socketio.emit("strategy_update", state, room=f"user_{user.id}")
                        logger.info("✅ Emitted updated strategy state for user %s", user_obj.email)
                    except Exception as e:
                        logger.error("❌ emit error for %s: %s", user.email, e)

            # When Shoonya socket opens, subscribe all active tokens
            def socket_open_callback():
                tokens = [
                    script.token
                    for strat in user_obj.strategies
                    for script in strat.scripts
                    if script.status not in ("Sold-out","Archived") and script.token
                ]
                if tokens:
                    logger.info("⚙️ Subscribing to %d tokens for %s", len(tokens), user.email)
                    subscribe_to_market_data(user_obj, tokens)
                else:
                    logger.warning("⚠️ No tokens to subscribe for %s", user.email)

        # Connection retry loop
        backoff = 1
        while True:
            try:
                api.start_websocket(
                    order_update_callback=order_update_callback,
                    subscribe_callback=subscribe_callback,
                    socket_open_callback=socket_open_callback,
                )
                logger.info("✅ Shoonya websocket running for %s", user.email)
                break
            except Exception as e:
                logger.error("❌ Websocket connect failed for %s: %s", user.email, e)
                socketio.sleep(backoff)
                backoff = min(backoff * 2, 60)

        # Keep the greenlet alive
        while True:
            socketio.sleep(1)

    # Launch the background task
    if not app.config.get("TESTING", False):
        socketio.start_background_task(run_ws)
        logger.info("✅ Launched websocket task for user %s", user.email)
    else:
        logger.info("✋ Skipping websocket in TEST mode for %s", user.email)
# ----------------------------
# Route: subscribe_to_market_data
# ----------------------------
def subscribe_to_market_data(user, tokens):
    """
    Subscribes to market data updates for the current user's tokens.
    """
    api = get_cached_api_instance(user)
    try:
        # First, format the tokens.
        formatted_tokens = [f"NSE|{token}" for token in tokens]
        logger.info("Attempting to subscribe with tokens: %s", formatted_tokens)
        subscribe_result = api.subscribe(formatted_tokens)
        logger.info("✅ Subscribed to tokens: %s for user %s. Response: %s",
                    formatted_tokens, user.email, subscribe_result)
    except Exception as e:
        logger.error("❌ Error subscribing to tokens %s for user %s: %s", formatted_tokens, user.email, e)
# ----------------------------
# Route: Update the Subscription
# ----------------------------
def update_user_subscription(user):
    """
    Update the persistent websocket connection for the given user by subscribing to the full set of tokens.
    This function rebuilds the strategy state and then calls the subscription method on the websocket
    connection (stored in persistent_ws_connections) with the updated list of tokens.
    """
    # Rebuild the current strategy state; this should include all scripts and their tokens.
    # refreshed_user = User.query.get(user.id)
    refreshed_user = db.session.get(User, user.id)
    state = build_strategy_state(refreshed_user)
    tokens = [data.get("token") for data in state.values() if data.get("token")]
    if not tokens:
        logger.warning("No tokens available in the strategy state for user %s", user.email)
        return
    subscribe_to_market_data(user, tokens)
    logger.info("Updated subscription for user %s with tokens: %s", user.email, tokens)

# ----------------------------
# Route: daily_prices_api
# ----------------------------
@app.route('/daily_prices_api', methods=['GET'])
@login_required
def daily_prices_api():
    tradingsymbol = request.args.get('tradingsymbol', '')
    from_date = request.args.get('from_date', '')  # expect Unix timestamp (as string)
    to_date = request.args.get('to_date', None)
    api = get_cached_api_instance(current_user)    
    if not api:
        return jsonify({"error": "Shoonya API initialization failed"}), 500
    result = api_daily_price_series(api, tradingsymbol, from_date, to_date)
    if result is None:
        return jsonify({"error": "Error fetching daily prices"}), 500
    return jsonify(result)
# ----------------------------
# Route: trigger_order
# ----------------------------
@app.route('/trigger_order', methods=['POST'])
@login_required
def trigger_order_route():
    # This endpoint triggers an order immediately.
    order_type = request.form.get('order_type')  # "entry", "re-entry", or "exit"
    tradingsymbol = request.form.get('tradingsymbol', 'RELIANCE')  # example value
    quantity = int(request.form.get('quantity', 10))
    api = get_cached_api_instance(current_user)
    if not api:
        flash("Shoonya API initialization failed.", "danger")
        return redirect(url_for('dashboard'))
    order_params = {
        "buy_or_sell": "B" if order_type in ["entry", "re-entry"] else "S",
        "product_type": "C",
        "exchange": "NSE",
        "tradingsymbol": tradingsymbol,
        "quantity": quantity,
        "discloseqty": 0,
        "price_type": "MKT",
        "price": 0,
        "trigger_price": None,
        "retention": "DAY",
        "amo": "NO",
        "remarks": "Algo triggered order"
    }
    result = place_order(api, **order_params)
    if result is None:
        flash("Order placement failed.", "danger")
    else:
        flash("Order placed successfully.", "success")
    return redirect(url_for('dashboard'))

@app.route('/simulate_tick', methods=['GET'])
@login_required
def simulate_tick_endpoint():
    token = request.args.get("token", "").strip()
    ltp_str = request.args.get("ltp", "").strip()
    if not token or not ltp_str:
        return jsonify({"error": "Both token and ltp parameters are required"}), 400
    try:
        ltp = float(ltp_str)
    except ValueError:
        return jsonify({"error": "Invalid ltp value"}), 400
    # Construct the message with correct key names
    message = {"tk": token, "lp": ltp}
    app.logger.info("✅ Simulating tick for token %s with LTP %s", token, ltp)
    on_data(current_user, message)
    
    # Read the updated strategy state from Redis (Just test is LTP Reflecting on Forntend)
    state_key = get_strategy_state_key(current_user.id)
    state_raw = redis_client.get(state_key)
    if state_raw:
        try:
            updated_state = json.loads(state_raw.decode())
            # Emit updated state to the client so that the frontend is refreshed
            socketio.emit("strategy_update", updated_state, room=str(current_user.id))
            app.logger.info("✅ Emitted updated strategy state for user %s", current_user.email)
        except Exception as e:
            app.logger.error("❌ Error emitting updated strategy state for user %s: %s", current_user.email, e)
    return jsonify({"message": "Simulated tick processed", "tk": token, "lp": ltp})
#------------------------------
# Route: Enhancing the Backtesting Module
# ----------------------------
def simulate_backtest(historical_data, params):
    # Parse historical data
    records = []
    for item in historical_data:
        try:
            rec = json.loads(item)
            # Expecting the date format to be like "28-MAR-2025"
            rec["date_obj"] = datetime.strptime(rec.get("time", ""), "%d-%b-%Y")
            records.append(rec)
        except Exception as e:
            continue

    if not records:
        return {"pnl": 0, "trade_count": 0, "trades": []}

    # Sort records in ascending order (oldest first)
    records.sort(key=lambda r: r["date_obj"])

    # Mapping of basis to field names
    field_map = {
        "open": "into",
        "high": "inth",
        "low": "intl",
        "close": "intc"
    }
    entry_field = field_map.get(params.get("entry_basis", "close").lower(), "intc")

    # Initialize simulation state
    trades = []
    position_open = False
    cumulative_qty = 0
    weighted_avg_price = 0
    last_buy_price = None
    total_pnl = 0
    trade_count = 0
    last_entry_date = None  # To enforce one entry per day

    # Loop through historical records starting from the second day
    for i in range(1, len(records)):
        prev_rec = records[i - 1]
        curr_rec = records[i]
        trade_date = curr_rec["date_obj"].date()

        # Get baseline price from previous day using the selected entry basis
        try:
            baseline_price = float(prev_rec.get(entry_field, 0))
            current_price = float(curr_rec.get(entry_field, 0))
        except Exception:
            continue

        # --- ENTRY PHASE: If no position is open ---
        if not position_open:
            # Enforce one entry per day: if already entered today, skip
            if last_entry_date == trade_date:
                continue

            # Use previous day's value as threshold
            threshold = baseline_price
            if params.get("entry_percentage", 0) < 0:
                desired_entry = threshold * (1 - abs(params["entry_percentage"]) / 100)
                condition_met = current_price <= desired_entry
            else:
                desired_entry = threshold * (1 + abs(params["entry_percentage"]) / 100)
                condition_met = current_price >= desired_entry

            if condition_met:
                # Determine quantity based on investment type
                if params.get("investment_type", "quantity").lower() == "quantity":
                    qty = int(params.get("investment_value", 0))
                else:
                    qty = int(params.get("investment_value", 0) / current_price) if current_price > 0 else 0

                trades.append({
                    "date": trade_date.strftime("%Y-%m-%d"),
                    "action": "BUY",
                    "price": current_price,
                    "quantity": qty,
                    "cumulative_qty": qty,
                    "weighted_avg_price": current_price
                })
                position_open = True
                cumulative_qty = qty
                weighted_avg_price = current_price
                last_buy_price = current_price
                last_entry_date = trade_date
                trade_count += 1
                # Continue to next day after an entry
                continue

        # --- RE-ENTRY PHASE: If position is open and today is after the initial entry day ---
        if position_open and trade_date > last_entry_date:
            reentry_triggered = False
            reentry_types_triggered = []
            # Retrieve reentry parameters
            reentry_params = params.get("reentry_params", {})

            # Check based on "prev_day" condition (if defined)
            if "prev_day" in reentry_params:
                reentry_pct = reentry_params["prev_day"].get("percentage", 0)
                # Re-fetch threshold for previous day if needed (for simulation, we use baseline_price)
                if reentry_pct < 0:
                    desired_reentry = baseline_price * (1 - abs(reentry_pct) / 100)
                    if current_price <= desired_reentry:
                        reentry_triggered = True
                        reentry_types_triggered.append("prev_day")
                else:
                    desired_reentry = baseline_price * (1 + abs(reentry_pct) / 100)
                    if current_price >= desired_reentry:
                        reentry_triggered = True
                        reentry_types_triggered.append("prev_day")

            # Check based on "last_buy" condition
            if "last_buy" in reentry_params and last_buy_price:
                reentry_pct = reentry_params["last_buy"].get("percentage", 0)
                if reentry_pct < 0:
                    desired_last_buy = last_buy_price * (1 - abs(reentry_pct) / 100)
                    if current_price <= desired_last_buy:
                        reentry_triggered = True
                        reentry_types_triggered.append("last_buy")
                else:
                    desired_last_buy = last_buy_price * (1 + abs(reentry_pct) / 100)
                    if current_price >= desired_last_buy:
                        reentry_triggered = True
                        reentry_types_triggered.append("last_buy")

            # Check based on "weighted_avg" condition
            if "weighted_avg" in reentry_params and weighted_avg_price:
                reentry_pct = reentry_params["weighted_avg"].get("percentage", 0)
                if reentry_pct < 0:
                    desired_weighted = weighted_avg_price * (1 - abs(reentry_pct) / 100)
                    if current_price <= desired_weighted:
                        reentry_triggered = True
                        reentry_types_triggered.append("weighted_avg")
                else:
                    desired_weighted = weighted_avg_price * (1 + abs(reentry_pct) / 100)
                    if current_price >= desired_weighted:
                        reentry_triggered = True
                        reentry_types_triggered.append("weighted_avg")

            if reentry_triggered:
                # Determine additional quantity
                if params.get("investment_type", "quantity").lower() == "quantity":
                    qty = int(params.get("investment_value", 0))
                else:
                    qty = int(params.get("investment_value", 0) / current_price) if current_price > 0 else 0

                trades.append({
                    "date": trade_date.strftime("%Y-%m-%d"),
                    "action": "BUY (Re-entry: " + ", ".join(reentry_types_triggered) + ")",
                    "price": current_price,
                    "quantity": qty,
                    "cumulative_qty": cumulative_qty + qty,
                    # Weighted average will be updated below
                })
                # Update cumulative quantity and weighted average price
                total_cost = weighted_avg_price * cumulative_qty + current_price * qty
                cumulative_qty += qty
                weighted_avg_price = total_cost / cumulative_qty if cumulative_qty > 0 else current_price
                last_buy_price = current_price
                trade_count += 1
                # Continue to next day after re-entry
                continue

        # --- EXIT PHASE (Not detailed here, but would check if current_price meets profit or stop loss conditions) ---
        # You would add logic here to trigger a SELL order, record the trade, and reset the state.
        # For this example, we assume exit is handled elsewhere.

    return {
        "pnl": total_pnl,
        "trade_count": trade_count,
        "trades": trades
    }

# ----------------------------
# Route: backtest # In your /backtest route in main.py, after fetching historical data:
# ----------------------------
@app.route('/backtest', methods=['GET', 'POST'])
@login_required
def backtest():
    if request.method == 'POST':
        # Get strategy parameters from form (similar to live trading)
        script = request.form['script'].strip()
        entry_basis = request.form.get('entry_basis', 'close')
        try:
            entry_percentage = float(request.form.get('entry_percentage', 0))
        except ValueError:
            entry_percentage = 0
        reentry_params_str = request.form.get('reentry_params', '{}')
        try:
            reentry_params = json.loads(reentry_params_str)
        except Exception:
            reentry_params = {}
        investment_type = request.form.get('investment_type', 'quantity')
        try:
            investment_value = float(request.form.get('investment_value', 0))
        except ValueError:
            investment_value = 0
        profit_target_type = request.form.get('profit_target_type', 'percentage')
        try:
            profit_target_value = float(request.form.get('profit_target_value', 0))
        except ValueError:
            profit_target_value = 0
        stop_loss_type = request.form.get('stop_loss_type', 'percentage')
        try:
            stop_loss_value = float(request.form.get('stop_loss_value', 0))
        except ValueError:
            stop_loss_value = 0
        execution_time_type = request.form.get('execution_time_type', 'before')
        execution_time_value = request.form.get('execution_time_value', '10:00')
        execution_time = f"{execution_time_type} {execution_time_value}"
        
        # Parse date range from HTML5 date input (format YYYY-MM-DD)
        start_date_str = request.form.get('start_date')
        end_date_str = request.form.get('end_date')
        if not start_date_str:
            flash("Start date is required.", "danger")
            return redirect(url_for('backtest'))
        start_date_obj = datetime.strptime(start_date_str, "%Y-%m-%d")
        end_date_obj = datetime.strptime(end_date_str, "%Y-%m-%d") if end_date_str else datetime.now()
        
        # Limit backtest to 1 year
        if (end_date_obj - start_date_obj).days > 365:
            flash("Backtesting can only be done for a maximum of 1 year.", "danger")
            return redirect(url_for('backtest'))
        
        # Convert dates to Unix timestamps
        start_timestamp = int(start_date_obj.timestamp())
        end_timestamp = int(end_date_obj.timestamp())
        
        # Initialize API instance to fetch historical data
        api = get_cached_api_instance(current_user)
        if not api:
            flash("Shoonya API initialization failed.", "danger")
            return redirect(url_for('backtest'))
        
        # Fetch historical data
        historical_data = api_daily_price_series(api, script, start_timestamp, end_timestamp)
        if not historical_data:
            flash("Failed to retrieve historical data.", "danger")
            return redirect(url_for('backtest'))
        
        # Bundle simulation parameters
        sim_params = {
            "entry_basis": entry_basis,
            "entry_percentage": entry_percentage,
            "reentry_params": reentry_params,
            "investment_type": investment_type,
            "investment_value": investment_value,
            "profit_target_type": profit_target_type,
            "profit_target_value": profit_target_value,
            "stop_loss_type": stop_loss_type,
            "stop_loss_value": stop_loss_value,
            "execution_time": execution_time
        }
        
        # Run simulation using our simulation function
        simulation_result = simulate_backtest(historical_data, sim_params)
        flash("Backtesting completed.", "success")
        return render_template('backtest.html', result=simulation_result)
    
    return render_template('backtest.html', result=None)

# ----------------------------
# Route: Calender
# ----------------------------
def convert_date_to_unix(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return int(dt.timestamp())

# Use gevent-friendly sleep and SocketIO background task
def send_heartbeat():
    while True:
        try:
            socketio.emit("heartbeat", {"msg": "ping"})
        except Exception as e:
            logger.error("❌ WebSocket heartbeat failed: %s", e)
        # gevent-safe sleep
        socketio.sleep(30)

# Graceful shutdown on SIGINT/SIGTERM
def _graceful_exit(signum, frame):
    logger.info(f"Received exit signal {signum}; shutting down Flask+SocketIO…")
    try:
        socketio.stop()
    except Exception:
        pass
    try:
        redis_client.close()
    except Exception:
        pass
    sys.exit(0)
# ----------------------------
# Main Entry Point
# ----------------------------
@app.route('/')
def index():
    # Redirect to dashboard (or you could render a landing page)
    return redirect(url_for('dashboard'))

# At module top
_background_tasks_started = False

# Add this to the bottom of your main.py (below all route definitions), then remove the  __main__block.
def startup_existing_websockets():
    """Scan Redis for existing users and start their persistent websockets."""
    with app.app_context():
        for key in redis_client.keys("user:*:strategy_state"):
            try:
                user_id = int(key.decode().split(":")[1])
                user = db.session.get(User, user_id)
                if user:
                    socketio.start_background_task(start_shoonya_websocket, user)
                    logger.info("✅ Started websocket for user %s", user_id)
            except Exception as e:
                logger.error("❌ Error starting websocket for key %s: %s", key, e)

@socketio.on('connect')
def _start_background_on_connect():
    global _background_tasks_started
    if not _background_tasks_started:
        _background_tasks_started = True
        # 1) Heartbeat
        socketio.start_background_task(send_heartbeat)
        # 2) State logger
        socketio.start_background_task(log_strategy_state_periodically)
        # 3) Kick off existing-user websockets (skip in tests)
        if not app.config.get("TESTING", False):
            socketio.start_background_task(startup_existing_websockets)

@socketio.on('disconnect')
def handle_disconnect():
    # Only clear if we know who disconnected
    if not current_user.is_authenticated:
        return

    with ws_lock:
        if current_user.id in persistent_ws_connections:
            persistent_ws_connections.pop(current_user.id)
            logger.info("Cleared websocket flag for user %s on disconnect", current_user.id)

@app.after_request
def set_security_headers(response):
    # Prevent click-jacking
    response.headers["X-Frame-Options"] = "DENY"
    # Prevent MIME sniffing
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Enforce HTTPS for a year
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # Control referrer leakage
    response.headers["Referrer-Policy"] = "no-referrer-when-downgrade"
    return response
