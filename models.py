# models.py

import json
from datetime import datetime, date
from flask_login import UserMixin
from .extensions import db, bcrypt  # use the shared extension instances

class User(UserMixin, db.Model):
    __tablename__ = 'user'
    id       = db.Column(db.Integer, primary_key=True)
    email    = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(256), nullable=False)

    api_credential = db.relationship('APICredential', backref='user', uselist=False)
    strategies     = db.relationship('StrategySet', backref='user', lazy=True)

    def set_password(self, raw_pw):
        self.password = bcrypt.generate_password_hash(raw_pw).decode('utf-8')

    def check_password(self, raw_pw):
        return bcrypt.check_password_hash(self.password, raw_pw)


class APICredential(db.Model):
    __tablename__ = 'api_credential'
    id                = db.Column(db.Integer, primary_key=True)
    shoonya_user_id   = db.Column(db.String(50))
    _shoonya_password = db.Column("shoonya_password", db.Text,   nullable=True)
    vendor_code       = db.Column(db.String(50))
    _api_secret       = db.Column("api_secret",       db.Text,   nullable=True)
    imei              = db.Column(db.String(50))
    _totp_secret      = db.Column("totp_secret",      db.Text,   nullable=True)
    user_id           = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    __table_args__  = (
        db.Index('idx_api_credential_user_id', 'user_id'),
    )

    @property
    def shoonya_password(self):
        from main import fernet
        return fernet.decrypt(self._shoonya_password.encode()).decode()

    @shoonya_password.setter
    def shoonya_password(self, raw_text):
        from main import fernet
        self._shoonya_password = fernet.encrypt(raw_text.encode()).decode()

    @property
    def api_secret(self):
        from main import fernet
        return fernet.decrypt(self._api_secret.encode()).decode()

    @api_secret.setter
    def api_secret(self, raw_text):
        from main import fernet
        self._api_secret = fernet.encrypt(raw_text.encode()).decode()

    @property
    def totp_secret(self):
        from main import fernet
        return fernet.decrypt(self._totp_secret.encode()).decode()

    @totp_secret.setter
    def totp_secret(self, raw_text):
        from main import fernet
        self._totp_secret = fernet.encrypt(raw_text.encode()).decode()


class StrategySet(db.Model):
    __tablename__ = 'strategy_set'
    __table_args__ = (
        db.Index('idx_strategy_set_user_id', 'user_id'),
        db.Index('idx_strategy_set_user_status', 'user_id', 'status'),
    )
    id                  = db.Column(db.Integer, primary_key=True)
    name                = db.Column(db.String(100), nullable=False)
    created_date        = db.Column(db.DateTime, default=datetime.utcnow)
    status              = db.Column(db.String(50), default="Waiting...")
    entry_basis         = db.Column(db.String(20))
    entry_percentage    = db.Column(db.Float)
    investment_type     = db.Column(db.String(20))
    investment_value    = db.Column(db.Float)
    profit_target_type  = db.Column(db.String(20))
    profit_target_value = db.Column(db.Float)
    stop_loss_type      = db.Column(db.String(20))
    stop_loss_value     = db.Column(db.Float)
    execution_time      = db.Column(db.String(20))
    reentry_params      = db.Column(db.Text)
    user_id             = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    scripts = db.relationship('StrategyScript', backref='strategy_set', lazy=True)


class StrategyScript(db.Model):
    __tablename__ = 'strategy_script'
    id                      = db.Column(db.Integer, primary_key=True)
    script_name             = db.Column(db.String(100), nullable=False)
    token                   = db.Column(db.String(100))
    ltp                     = db.Column(db.Float, nullable=True, default=0)
    status                  = db.Column(db.String(50), default="Waiting")
    last_trade_date         = db.Column(db.Date, nullable=True)
    entry_threshold         = db.Column(db.Float, nullable=True)
    entry_threshold_date    = db.Column(db.Date,  nullable=True)
    reentry_threshold       = db.Column(db.Float, nullable=True)
    reentry_threshold_date  = db.Column(db.Date,  nullable=True)
    last_buy_price          = db.Column(db.Float, nullable=True)
    weighted_avg_price      = db.Column(db.Float, nullable=True)
    last_entry_date         = db.Column(db.Date, nullable=True)
    last_order_time         = db.Column(db.DateTime, nullable=True)
    cumulative_qty          = db.Column(db.Float, nullable=True, default=0)
    failure_timestamp       = db.Column(db.Float, nullable=True)
    trade_count             = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    strategy_set_id         = db.Column(db.Integer, db.ForeignKey('strategy_set.id'), nullable=False, index=True)
    __table_args__ = (
        # index lookups by strategy_set_id
        db.Index('idx_strategy_script_set_id', 'strategy_set_id'),
        # index filtering by strategy_set_id + status
        db.Index('idx_strategy_script_set_id_status', 'strategy_set_id', 'status'),
    )

# archive model
class StrategyScriptArchive(db.Model):
    """
    To prevent unbounded table growth, let’s move scripts (and their state) older than, say, 30 days into an archive table nightly at 2 AM IST.
    """
    __tablename__ = 'strategy_script_archive'
    id               = db.Column(db.Integer, primary_key=True)
    original_id      = db.Column(db.Integer, nullable=False)
    strategy_set_id  = db.Column(db.Integer, nullable=False)
    script_name      = db.Column(db.String(50), nullable=False)
    archived_at      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # store any extra fields you might need later:
    data             = db.Column(db.JSON, nullable=False)
