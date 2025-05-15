import pytest
from datetime import datetime, timedelta
from main import db
from app.models import User, StrategySet, StrategyScript, StrategyScriptArchive
from app.tasks import archive_old_scripts_task
from datetime import date, timedelta

@pytest.fixture
def scripts_to_archive(test_app):
    # 1) Create a user (only email in ctor) and set a password via the model helper
    user = User(email="u@x.com")
    user.set_password("irrelevant-for-test")
    db.session.add(user)
    # fill all required StrategySet fields and link to that user
    strat = StrategySet(
        name="ArchiveTest",
        entry_basis="close",
        entry_percentage=1.0,
        investment_type="quantity",
        investment_value=1,
        profit_target_type="percentage",
        profit_target_value=1,
        stop_loss_type="percentage",
        stop_loss_value=1,
        execution_time="09:00",
        reentry_params="{}",
        user=user
    )
    db.session.add(strat)
    db.session.commit()

    # 2) Now create two scripts tied to that strategy:
    cutoff = date.today() - timedelta(days=30)
    old = StrategyScript(
        script_name="OLD", token="T1",
        strategy_set=strat,
        status="Sold-out",
        last_trade_date=cutoff - timedelta(days=1)
    )
    new = StrategyScript(
        script_name="NEW", token="T2",
        strategy_set=strat,
        # either not "Sold-out" or last_trade_date >= cutoff
        status="Sold-out",
        last_trade_date=date.today()  # too recent to archive
    )
    db.session.add_all([old, new])
    db.session.commit()

    # 3) Manually back-date `old` by 31 days, leave `new` as-is
    old.updated_at = datetime.utcnow() - timedelta(days=31)
    new.updated_at = datetime.utcnow()
    db.session.commit()

    return old, new

def test_archive_old_scripts_task(scripts_to_archive):
    old, new = scripts_to_archive

    # Run the archival task synchronously
    archive_old_scripts_task.run()

    # Old script should have been moved into the archive table
    archived = StrategyScriptArchive.query.filter_by(script_name="OLD").one_or_none()
    assert archived is not None, "Old scripts older than 30 days must be archived"

    # New script should still exist in the live table
    still_there = StrategyScript.query.filter_by(script_name="NEW").one_or_none()
    assert still_there is not None, "New scripts (less than 30 days) must remain"
