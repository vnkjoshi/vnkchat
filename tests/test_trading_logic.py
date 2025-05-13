import os, sys
# → add project root (one level up) to Python path so imports work
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from datetime import datetime, timedelta
from app.shoonya_integration import get_previous_ohlc
from app.strategies import evaluate_trade_decision

class DummyAPI:
    def __init__(self, series):
        # series: list of JSON strings
        self._series = series
    def get_daily_price_series(self, **kwargs):
        return self._series
    def login(self, **kwargs): pass

class DummyScript:
    def __init__(self, status, **kwargs):
        # required by evaluate_trade_decision logging
        self.script_name = kwargs.get('script_name', 'TEST')
        self.status = status

        # Entry thresholds
        self.entry_threshold = kwargs.get('entry_threshold')
        self.entry_threshold_date = kwargs.get('entry_threshold_date')

        # Re-entry thresholds
        self.reentry_threshold = kwargs.get('reentry_threshold')
        self.reentry_threshold_date = kwargs.get('reentry_threshold_date')

        # Running‐position fields
        self.weighted_avg_price = kwargs.get('weighted_avg_price')
        self.last_trade_date = kwargs.get('last_trade_date')
        self.last_entry_date = kwargs.get('last_entry_date')
        # fields for post-fill logic skipped here

class DummyStrategy:
    def __init__(self, entry_basis, entry_percentage, profit_target_type='percentage',
                 profit_target_value=0, stop_loss_type='percentage', stop_loss_value=0,
                 reentry_params='{}'):
        self.entry_basis = entry_basis
        self.entry_percentage = entry_percentage
        self.profit_target_type = profit_target_type
        self.profit_target_value = profit_target_value
        self.stop_loss_type = stop_loss_type
        self.stop_loss_value = stop_loss_value
        self.reentry_params = reentry_params

@pytest.fixture
def yesterday_series(tmp_path):
    # two-day JSON series: yesterday then day before
    base = datetime.now().date() - timedelta(days=1)
    recs = [
        '{"time":"%s","intc":100}' % (base.strftime("%d-%b-%Y")),
        '{"time":"%s","intc":90}'  % ((base - timedelta(days=1)).strftime("%d-%b-%Y"))
    ]
    return recs

def test_get_previous_ohlc_basic(yesterday_series):
    api = DummyAPI(series=yesterday_series)
    val = get_previous_ohlc(api, tradingsymbol='FOO', field='close', exchange='NSE')
    assert pytest.approx(val, rel=1e-6) == 100

def test_evaluate_trade_entry():
    now = datetime.now()
    # script waiting, threshold already set to 100 today
    script = DummyScript(
        status="Waiting",
        entry_threshold=100,
        entry_threshold_date=now.date()
    )
    strat = DummyStrategy(entry_basis='close', entry_percentage=5)
    # live_ltp ≥ 100 * 1.05 => BUY
    decision = evaluate_trade_decision(None, script, live_ltp=106, strategy=strat, now=now)
    assert decision == "BUY"

def test_evaluate_trade_exit():
    now = datetime.now()
    # script running since yesterday, weighted avg=100, profit target +10%
    script = DummyScript(
        status="Running",
        weighted_avg_price=100,
        last_trade_date=now.date() - timedelta(days=1)
    )
    strat = DummyStrategy(
        entry_basis='close',
        entry_percentage=0,
        profit_target_type='percentage',
        profit_target_value=10,
        stop_loss_type='percentage',
        stop_loss_value=0
    )
    # live_ltp ≥ 110 => SELL
    decision = evaluate_trade_decision(None, script, live_ltp=111, strategy=strat, now=now)
    assert decision == "SELL"

def test_evaluate_trade_no_action():
    now = datetime.now()
    script = DummyScript(status="Waiting")
    strat  = DummyStrategy(entry_basis='close', entry_percentage=5)
    # live_ltp below threshold
    decision = evaluate_trade_decision(None, script, live_ltp=90, strategy=strat, now=now)
    assert decision == "NONE"

"""
# Run the tests
pytest tests/test_trading_logic.py -q
"""