# config.py
import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
ENV      = os.getenv('FLASK_ENV', 'development').lower()

class ConfigError(Exception):
    """Raised when a required env var is missing in production."""
    pass

class BaseConfig:
    # -----------------------
    # SECRET KEY
    # -----------------------
    SECRET_KEY = os.getenv('FLASK_SECRET_KEY', 'dev-secret')
    if ENV == 'production' and not os.getenv('FLASK_SECRET_KEY'):
        raise ConfigError("FLASK_SECRET_KEY is required in production")

    # -----------------------
    # Database
    # -----------------------
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_DATABASE_URI = os.getenv(
        'DATABASE_URL',
        # only used in non‐production
        'sqlite:///' + os.path.join(BASE_DIR, 'dev.db')
    )
    if ENV == 'production' and not os.getenv('DATABASE_URL'):
        raise ConfigError("DATABASE_URL is required in production")

    # -----------------------
    # Fernet (encrypting credentials)
    # -----------------------
    FERNET_KEY = os.getenv('FERNET_KEY')
    if ENV == 'production' and not FERNET_KEY:
        raise ConfigError("FERNET_KEY is required in production")

    # -----------------------
    # Redis (Celery broker & state cache)
    # -----------------------
    REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/2')
    if ENV == 'production' and not os.getenv('REDIS_URL'):
        raise ConfigError("REDIS_URL is required in production")

    # -----------------------
    # Flask-Limiter storage backend
    # -----------------------
    # Default to in-memory (so tests/dev won’t hit Redis),
    # override in Docker via env var.
    RATELIMIT_STORAGE_URI = os.getenv('RATELIMIT_STORAGE_URI', 'memory://')

    CELERY_BROKER_URL     = os.getenv('CELERY_BROKER_URL',    'redis://localhost:6379/0')
    CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND','redis://localhost:6379/1')

    API_INSTANCE_TIMEOUT  = int(os.getenv('API_INSTANCE_TIMEOUT', 300))

    # -----------------------
    # Celery eager execution (disable to let worker run tasks)
    # -----------------------
    CELERY_TASK_ALWAYS_EAGER     = False
    CELERY_TASK_EAGER_PROPAGATES = False

    # -----------------------
    # Celery timezone & scheduling
    # -----------------------
    CELERY_TIMEZONE   = 'Asia/Kolkata'
    CELERY_ENABLE_UTC = False
    
    # How long to block duplicate orders (seconds)
    ORDER_COOLDOWN_SECONDS    = int(os.getenv("ORDER_COOLDOWN_SECONDS", 600))
    # How long to skip a script after a failure (seconds)
    FAILURE_COOLDOWN_SECONDS = int(os.getenv("FAILURE_COOLDOWN_SECONDS", 60))
    EVAL_SCHEDULE_SECONDS    = float(os.getenv("EVAL_SCHEDULE_SECONDS", 30.0))
    MARKET_START_HOUR = int(os.getenv("MARKET_START_HOUR", 9))
    MARKET_END_HOUR   = int(os.getenv("MARKET_END_HOUR", 15))

    # -----------------------
    # Feature flags
    # -----------------------
    FEATURE_FLAGS = {
        'enable_reentry': os.getenv('FLAG_REENTRY', 'true').lower() == 'true',
        'enable_healthz': os.getenv('FLAG_HEALTHZ', 'true').lower() == 'true',
    }

class DevelopmentConfig(BaseConfig):
    DEBUG = True
    # during local dev you can set these to True if you really want inline behavior
    # CELERY_TASK_ALWAYS_EAGER     = True
    # CELERY_TASK_EAGER_PROPAGATES = True

class ProductionConfig(BaseConfig):
    DEBUG = False

class TestingConfig(BaseConfig):
    # enable Flask’s testing mode
    TESTING = True
    DEBUG = False

    # use in-memory SQLite so each test process is fresh
    SQLALCHEMY_DATABASE_URI = os.getenv(
        'DATABASE_URL_TEST',
        'sqlite:///:memory:'
    )

    # point Celery at a separate Redis DB (so you can test task queuing)
    CELERY_BROKER_URL     = os.getenv(
        'CELERY_BROKER_URL_TEST',
        'redis://localhost:6379/3'
    )
    CELERY_RESULT_BACKEND = os.getenv(
        'CELERY_RESULT_BACKEND_TEST',
        'redis://localhost:6379/4'
    )

