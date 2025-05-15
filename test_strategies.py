import pytest
from datetime import datetime, date
from unittest.mock import Mock, patch
from app.strategies import evaluate_trade_decision

class DummyScript:
    def __init__(self):
        self.status = "Waiting"
        self.script_name = "ABC"
        self.entry_threshold = None
        self.entry_threshold_date = None
        self.reentry_threshold = None
        self.reentry_threshold_date = None
        self.weighted_avg_price = None
        self.last_entry_date = None
        self.last_trade_date = None
        self.last_buy_price = None
        self.user_id = 1
        self.id = 10

class DummyStrategy:
    entry_basis = "high"
    entry_percentage = 5
    profit_target_type = "percentage"
    profit_target_value = 10
    stop_loss_type = "percentage"
    stop_loss_value = 5
    reentry_params = '{"prev_day": {"basis": "high", "percentage": 2}}'

@pytest.fixture
def api_mock():
    return Mock()

@patch("app.strategies.get_previous_ohlc")
def test_evaluate_sets_entry_threshold(mock_ohlc, api_mock):
    # Arrange
    mock_ohlc.return_value = 100
    script = DummyScript()
    strategy = DummyStrategy()
    now = datetime(2025,5,8,10,0)
    # Act
    decision = evaluate_trade_decision(api_mock, script, live_ltp=105, strategy=strategy, now=now)
    # Assert
    assert decision == "BUY"
    assert script.entry_threshold == 100
    assert script.entry_threshold_date == date(2025,5,8)

@patch("app.strategies.get_previous_ohlc", side_effect=Exception("API fail"))
def test_evaluate_api_failure_skips(mock_ohlc, api_mock):
    script = DummyScript()
    strategy = DummyStrategy()
    now = datetime(2025,5,8,10,0)
    decision = evaluate_trade_decision(api_mock, script, live_ltp=105, strategy=strategy, now=now)
    assert decision == "NONE"
    # unchanged threshold
    assert script.entry_threshold is None
