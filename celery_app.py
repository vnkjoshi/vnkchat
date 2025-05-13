from gevent import monkey
monkey.patch_all()

import os
import time
from app import create_app
from celery import Celery
from celery.schedules import crontab
import celery.signals as _signals
from celery.signals import task_prerun, task_postrun, task_failure
from prometheus_client import start_http_server
from app.metrics import TASK_COUNTER, TASK_DURATION
from flask import current_app
from app.extensions import db

# Expose metrics on port 8000 (or any free port)
app = create_app()

# expose Prometheus metrics
start_http_server(8000)

# Determine and Use /data for Celery beat schedule (must match your Docker volume)
beat_dir = os.getenv('CELERY_BEAT_SCHEDULE_DIR', '/data')
# Ensure the directory exists (shared via Docker volume)
os.makedirs(beat_dir, exist_ok=True)

# Create the Celery instance with explicit new‐style keys
celery = Celery (
    app.import_name,
    broker=app.config['CELERY_BROKER_URL'],
    backend=app.config['CELERY_RESULT_BACKEND'],
    include=['app.tasks']
)

# Tell Celery whether to run tasks eagerly or not
celery.conf.update(
    task_always_eager    = app.config.get('CELERY_TASK_ALWAYS_EAGER', False),
    task_eager_propagates= app.config.get('CELERY_TASK_EAGER_PROPAGATES', False),
)

# Set up beat schedule file location
beat_schedule_file = os.path.join(beat_dir, 'celerybeat-schedule')
celery.conf.beat_schedule_filename = beat_schedule_file

# Configure Celery time settings
celery.conf.timezone    = app.config['CELERY_TIMEZONE']
celery.conf.enable_utc  = app.config['CELERY_ENABLE_UTC']

# Hook into Celery signals
@task_prerun.connect
def prerun_handler(sender=None, task_id=None, task=None, args=None, kwargs=None, **extras):
    # record wall-clock start timestamp
    task.__start_ts__ = time.time()

@task_postrun.connect
def postrun_handler(sender=None, task_id=None, task=None, args=None, kwargs=None,
                    retval=None, state=None, **extras):
    # compute elapsed time and observe
    start_ts = getattr(task, '__start_ts__', None)
    if start_ts is not None:
        elapsed = time.time() - start_ts
        TASK_DURATION.labels(task_name=sender.name).observe(elapsed)
    TASK_COUNTER.labels(task_name=sender.name, status='success').inc()

@task_failure.connect
def failure_handler(sender=None, task_id=None, exception=None, args=None, kwargs=None, **extras):
    # increment failure counter
    TASK_COUNTER.labels(task_name=sender.name, status='failure').inc()

# ────────────────────────────────────────────────────────────
# Global DB session teardown (prevents connection/session leaks)
# ────────────────────────────────────────────────────────────
@task_postrun.connect
def close_db_session_handler(sender=None, **kwargs):
    """
    Remove the SQLAlchemy session after every Celery task.
    """
    try:
        with app.app_context():
            db.session.remove()
    except Exception as e:
        current_app.logger.exception("❌ Error closing DB session: %s", e)
        
# Dynamically hook into worker_shutdown if it exists in this Celery install
_shutdown_signal = getattr(_signals, "worker_shutdown", None)
if _shutdown_signal:
    @_shutdown_signal.connect
    def _on_celery_shutdown(sig, how, **kwargs):
        # This will fire when the worker shuts down
        print(f"✅ Celery worker shutting down (how={how})")

# Build up the beat_schedule based on DEBUG flag
beat_schedule = {}
# Market‑hours schedule: every minute between 09:15–15:30 IST (Keep this in production)
if app.config.get('DEBUG'):
    # Development: fire every N seconds
    beat_schedule['evaluate-trading-conditions'] = {
        'task': 'app.tasks.evaluate_trading_conditions_task',
        'schedule': app.config['EVAL_SCHEDULE_SECONDS'], # secs from config
    }
else:
    # Production: only during market hours, Mon–Fri
    beat_schedule['evaluate-trading-conditions-market-hours'] = {
        'task': 'app.tasks.evaluate_trading_conditions_task',
        'schedule': crontab(
            minute='*/1',
            hour=f"{app.config['MARKET_START_HOUR']}-{app.config['MARKET_END_HOUR']}",
            day_of_week='mon-fri'
        ),
    }

# Schedule the task nightly
beat_schedule['archive-old-scripts-nightly'] = {
    'task': 'app.tasks.archive_old_scripts_task',
    'schedule': crontab(minute=0, hour='2'),
}

# Finally, apply the combined schedule to Celery
celery.conf.beat_schedule = beat_schedule

# Import tasks to register
from app import tasks  # Adjusted import path to match the project structure
