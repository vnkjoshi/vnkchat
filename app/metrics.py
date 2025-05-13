# metrics.py
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from flask import Response, request
import os

# HTTP request counter: labels = method, path, status
HTTP_REQUESTS = Counter(
    'http_requests_total',
    'Total HTTP requests',
    ['method', 'endpoint', 'http_status']
)

# Shoonya API call latency histogram: label = api_method
API_CALL_LATENCY = Histogram(
    'shoonya_api_call_duration_seconds',
    'Latency of calls to ShoonyaApi-py methods',
    ['api_method']
)

# Prometheus metrics for Celery
TASK_COUNTER   = Counter(
    'celery_tasks_total',
    'Total Celery tasks executed',
    ['task_name', 'status']  # status = success|failure
)

# Celery task duration histogram: task_name label
TASK_DURATION  = Histogram(
    'celery_task_duration_seconds',
    'Time spent processing Celery tasks',
    ['task_name']
)

# ─── Prometheus metrics ─────────────────────────────────────────────────
# Count of all Shoonya API errors, labeled by method name
SHOONYA_API_ERRORS = Counter(
    'shoonya_api_errors_total',
    'Total number of Shoonya API errors',
    ['api_method']
)

# Count of Celery task failures, labeled by task name
CELERY_TASK_FAILURES = Counter(
    'celery_task_failures_total',
    'Number of Celery task failures',
    ['task_name']
)

# Gauge for Celery queue length (via Redis broker)
CELERY_QUEUE_LENGTH = Gauge(
    'celery_queue_length',
    'Length of the Celery default queue',
    ['queue_name']
)
# ──────────────────────────────────────────────────────────────────────────