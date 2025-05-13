# ─── Builder Stage ───────────────────────────────────────────────────────
FROM python:3.11-slim-bullseye AS builder
WORKDIR /app

# ------------------ add user AND prepare /data ------------------
RUN groupadd -r app && useradd -r -g app app && mkdir -p /data && chown app:app /data

# 1) Install runtime deps
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# 2) Install dev & test deps, then copy & test
RUN apt-get update && apt-get install -y --no-install-recommends gcc libffi-dev build-essential curl && rm -rf /var/lib/apt/lists/*

COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY . .
RUN pytest --maxfail=1 --disable-warnings -q

# ─── Runtime Stage ───────────────────────────────────────────────────────
FROM python:3.11-slim-bullseye AS runtime
WORKDIR /app

# 3) Only install runtime deps
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# 4) Create non-root user and prepare /data for Celery Beat
RUN groupadd -r app && useradd -r -g app app && mkdir -p /data && chown app:app /data

# 5) Copy your application code
COPY --from=builder /app /app
RUN rm -rf tests/ .vscode/ requirements-dev.txt && find /app -type d -name "__pycache__" -exec rm -rf {} +

# 6) Switch to the app user
USER app

# 7) Expose ports
EXPOSE 5000 8000

# 8) Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD curl -f http://localhost:5000/healthz || exit 1

# 9) Start your app
CMD ["gunicorn", "-k", "geventwebsocket.gunicorn.workers.GeventWebSocketWorker", "--worker-connections", "1000", "--bind", "0.0.0.0:5000", "main:app"]
