import pytest
from main import db
from app.models import User, StrategySet, StrategyScript
from app.strategies import build_strategy_state

@pytest.fixture
def populated_user(test_app):
    # create one user → one strategy → one script
    user = User(email="u1@example.com")
    user.set_password("test-password")
    db.session.add(user)
    strat = StrategySet(
        name="S1",
        entry_basis="close",
        entry_percentage=1.5,
        investment_type="quantity",
        investment_value=5,
        profit_target_type="percentage",
        profit_target_value=10,
        stop_loss_type="percentage",
        stop_loss_value=2,
        execution_time="09:30",
        reentry_params="{}",
        user=user
    )
    db.session.add(strat)
    script = StrategyScript(
        script_name="FOO",
        token="ABC",
        strategy_set=strat,
        last_entry_date=None,
        last_trade_date=None,
        status="Waiting"
    )
    db.session.add(script)
    db.session.commit()
    return user, strat, script

def test_build_strategy_state_empty_user(test_app):
    u = User(email="empty@example.com")
    # not added to session, so no strategies
    state = build_strategy_state(u)
    assert state == {}

def test_build_strategy_state_populated(populated_user):
    user, strat, script = populated_user
    state = build_strategy_state(user)

    # The top‐level key is the script name, not the strategy ID
    key = script.script_name
    assert key in state, f"Expected script name {key!r} as state key"

    entry = state[key]
    # Verify the static configuration block
    cfg = entry["configuration"]
    assert cfg["entry_basis"] == strat.entry_basis
    assert cfg["entry_percentage"] == strat.entry_percentage
    assert cfg["execution_time"] == strat.execution_time

    # Verify dynamic fields match the model
    assert entry["status"] == script.status
    assert entry["total_quantity"] == script.cumulative_qty or 0
    assert entry["trade_count"] == (script.trade_count or 0)

