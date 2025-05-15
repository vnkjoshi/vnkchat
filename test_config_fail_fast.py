import os
import sys
import importlib
import pytest

def test_fail_fast_on_missing_env_vars(tmp_path, monkeypatch):
    # 1) Ensure required vars are NOT set
    for var in ("DATABASE_URL", "FLASK_SECRET_KEY", "FERNET_KEY", "REDIS_URL"):
        monkeypatch.delenv(var, raising=False)

    # 2) Reload main.py and assert it exits
    #    Note: if your app is named differently, adjust the module name
    if "main" in sys.modules:
        del sys.modules["main"]
    with pytest.raises(SystemExit) as excinfo:
        import main  # this should execute the fail‐fast check on import
        importlib.reload(main)
    assert excinfo.value.code == 1
