import pytest
from datetime import datetime, time
from zoneinfo import ZoneInfo
from app import strategies

def test_skips_outside_market_hours(monkeypatch):
    # 1) Force now() to be 08:00 IST (before market open)
    class DummyDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            # Return a time before market open
            return datetime(2025, 5, 1, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    # Patch the datetime used in strategies to our dummy
    monkeypatch.setattr(strategies, "datetime", DummyDateTime)

    # Spy on evaluate_trade_decision to ensure it never runs
    monkeypatch.setattr(
        strategies, "evaluate_trade_decision",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Should not run"))
    )

    # Run the trading cycle with no users (market-hours guard should return early)
    result = strategies.evaluate_trading_cycle(users=[])
    assert result is None
