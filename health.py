# app/routes/health.py

from flask import Blueprint, jsonify, current_app, request, Response
from sqlalchemy import text
import redis
from ..extensions import db
from app.metrics import HTTP_REQUESTS, CELERY_QUEUE_LENGTH, TASK_COUNTER, TASK_DURATION
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
import os

health_bp = Blueprint('health', __name__)

@health_bp.route('/healthz')
def healthz():
    if current_app.testing:
        return '', 200
    flags = current_app.config.get('FEATURE_FLAGS', {}) or {}
    if not flags.get('enable_healthz', False):
        return 'Not Implemented', 501

    status = {}
    code = 200
    try:
        db.session.execute(text('SELECT 1'))
        status['database'] = 'ok'
    except Exception as e:
        status['database'] = f'error: {e}'
        code = 500

    try:
        r = redis.Redis.from_url(current_app.config['REDIS_URL'])
        r.ping()
        status['redis'] = 'ok'
    except Exception as e:
        status['redis'] = f'error: {e}'
        code = 500

    return jsonify(status), code

@health_bp.route('/metrics')
def metrics():
    # basic auth, if you want to protect this endpoint
    auth = request.authorization
    if not auth or (auth.username, auth.password) != (
        os.getenv("METRICS_USER"), os.getenv("METRICS_PASSWORD")
    ):
        return Response(
            'Unauthorized', 401,
            {'WWW-Authenticate': 'Basic realm="Metrics"'}
        )

    # Collect all your counters/histograms
    HTTP_REQUESTS.collect()
    CELERY_QUEUE_LENGTH.collect()
    TASK_COUNTER.collect()
    TASK_DURATION.collect()
    # Render them in Prometheus text format
    output = generate_latest()
    return Response(output, mimetype=CONTENT_TYPE_LATEST)


"""
http://localhost:5000/healthz
http://localhost:8000/metrics
# ────────────────────────────────────────────────────────────

**Enable/disable at runtime**  
- To turn it **off**, set in your environment (or `.env`):  
  FLAG_HEALTHZ=false 

- To turn it **on**, omit it or set:  
  FLAG_HEALTHZ=true

This keeps your feature-toggles centralized in `config.py`, your helper trivial, and each endpoint can be turned on or off without any code deploy.
"""