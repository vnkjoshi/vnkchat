# Swing Algo Trading App

A modular, production-ready application for automating swing trading strategies using the Shoonya API. It provides a web UI, background workers, real-time updates, and built-in observability to help you deploy and monitor multiple trading scripts seamlessly.

---

## 🔍 Features

* **Web UI** (Flask + Socket.IO)

  * User authentication and management
  * API credentials management (securely encrypted in database)
  * Dashboard to view and manage deployed strategies
  * Real-time order & market data updates via WebSocket

* **Background Workers** (Celery + Redis)

  * Fetch entry thresholds and market data periodically
  * Evaluate custom trading conditions per script
  * Place and manage orders with idempotency safeguards
  * Archive inactive or outdated strategy scripts automatically

* **Data Layer**

  * SQLAlchemy ORM models for `User`, `APICredential`, `StrategySet`, `StrategyScript`
  * PostgreSQL (or any SQL database) as primary datastore
  * Redis for task queuing and shared caches

* **Observability & Metrics**

  * Prometheus metrics endpoint (`/metrics`) in the Flask app
  * Celery worker metrics exposed on port `8000`
  * Health-check endpoint (`/healthz`) for database and Redis
  * Structured logging with context (user\_id, task\_id, etc.)

* **Configuration & Secrets**

  * Environment-driven configuration (`config.py`)
  * Template `.env.example` for required variables
  * Secure management of Shoonya credentials via encrypted database storage

* **Deployment Ready**

  * Multi-stage Dockerfile for lean production images
  * Non-root user in containers for security
  * CI/CD hooks for linting, type checking (optional)

---

## 🚀 Quickstart

### 1. Prerequisites

* Python 3.11+
* Redis server
* PostgreSQL (or your preferred SQL database)
* (Optional) Docker & Docker Compose

### 2. Configuration

1. Copy and edit the environment template:

   ```bash
   cp .env.example .env
   # open .env in your editor and fill in values
   ```

2. Install Python dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Initialize the database:

   ```bash
   flask db upgrade
   ```

### 3. Running the Services

#### Without Docker

1. **Start Flask + Socket.IO**

   ```bash
   flask run
   ```

2. **Start Celery workers**

   ```bash
   celery -A celery_app:celery worker -P eventlet --loglevel=info
   ```

3. **Start Celery beat**

   ```bash
   celery -A celery_app:celery beat --loglevel=info
   ```

#### With Docker Compose

```bash
docker-compose up --build
```

*(This will start the web app, Redis, PostgreSQL, Celery workers, and beat scheduler.)*

### 4. Access the App

* **UI**: `http://localhost:5000`
* **Health Check**: `http://localhost:5000/healthz`
* **Metrics**: `http://localhost:5000/metrics`
* **Celery Metrics**: `http://localhost:8000`

---

## 📂 Project Structure

```
SwingAlgo/
├── app/                  # Application package
│   ├── __init__.py       # Flask app factory
│   ├── models.py         # SQLAlchemy models
│   ├── routes/           # Flask blueprints (health, auth, api, ws)
│   ├── tasks/            # Celery task modules (orders, market, archive)
│   ├── extensions.py     # DB, Redis, LoginManager, etc.
│   └── ...               # Other modules (utils, services)
├── config.py             # Configuration definitions
├── celery_app.py         # Celery app initialization
├── main.py               # Application entrypoint
├── requirements.txt
├── Dockerfile            # Multi-stage build for production
├── docker-compose.yml    # Orchestrates services for local development
├── migrations/           # Alembic database migrations
└── README.md             # This file
```

---

## 🧪 Testing & Quality

* **Unit Tests**: located under `tests/`, use `pytest`.
* **Linting**: run `flake8` and `black --check .` before commits.
* **Type Checking**: run `mypy app/` (optional but recommended).

---

## 🤝 Contributing

1. Fork the repo and create a feature branch: `git checkout -b feature/my-feature`
2. Commit your changes and push: `git push origin feature/my-feature`
3. Open a Pull Request with a clear description of your changes.

Please adhere to the existing code style and write tests for new features or bug fixes.

---

## 📜 License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

---

*Happy trading and code responsibly!*
