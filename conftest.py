import os, sys

# ─── FORCE TEST ENVIRONMENT ────────────────────────────────────────────────────
# NOTE: these must come before any 'import main' so that main.py sees them.
os.environ["SKIP_LOAD_DOTENV"]    = "1"
os.environ["DATABASE_URL"]         = "sqlite:///:memory:"
os.environ["FLASK_SECRET_KEY"]     = "test-secret"
os.environ["FERNET_KEY"]           = "tM5zWc1NMpEOMDEUth67U6uDNo0Ydp3Mia_UTR1G-UY="
os.environ["REDIS_URL"]            = "redis://localhost:6379/0"
# ───────────────────────────────────────────────────────────────────────────────

# Ensure imports like "import main" pick up your project’s modules
root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if root not in sys.path:
    sys.path.insert(0, root)

# ─── ALSO INSERT the app/ PACKAGE so “import strategies” -> app/strategies.py ───
app_path = os.path.join(root, "app")
if app_path not in sys.path:
    sys.path.insert(0, app_path)

import pytest
from main import app as _app, db as _db

@pytest.fixture(scope="session")
def test_app():
    """Flask app context using in-memory SQLite."""
    _app.config.update({
        "TESTING": True,
        # double-ensure we’re pointing at SQLite for tests
        "SQLALCHEMY_DATABASE_URI": os.environ["DATABASE_URL"],
        "WTF_CSRF_ENABLED": False,
    })
    with _app.app_context():
        _db.create_all()
        yield _app
        _db.drop_all()

@pytest.fixture
def client(test_app):
    return test_app.test_client()

@pytest.fixture
def runner(test_app):
    return test_app.test_cli_runner()
