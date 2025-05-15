import os
import logging
from flask import Flask
from .config import DevelopmentConfig, ProductionConfig
from .extensions import socketio, db, bcrypt, migrate, login_manager
from flask_wtf import CSRFProtect
from pythonjsonlogger import jsonlogger

def setup_structured_logging():
    handler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter(
        '%(asctime)s %(name)s %(levelname)s %(message)s %(user_id)s %(script_id)s'
    )
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

# Call it immediately
setup_structured_logging()

def create_app():
    # determine project root for templates/static
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

    # single Flask instantiation with correct folders
    app = Flask(
        __name__,
        instance_relative_config=False,
        template_folder=os.path.join(project_root, 'templates'),
        static_folder=os.path.join(project_root, 'static'),
    )
    
    # ⬇ prevent objects from being expired whenever you commit
    app.config['SQLALCHEMY_EXPIRE_ON_COMMIT'] = False

    # ─── Load config (including SECRET_KEY & FLASK_ENV) ───────────────────
    env = os.getenv('FLASK_ENV', 'development').lower()

    if env == 'production':
        app.config.from_object('app.config.ProductionConfig')
    elif env == 'testing':
        app.config.from_object('app.config.TestingConfig')
    else:
        app.config.from_object('app.config.DevelopmentConfig')


    # ─── Ensure CSRF is enabled ──────────────────────────────────────────
    app.config['WTF_CSRF_ENABLED'] = True

    # ─── Initialize Flask-WTF’s CSRFProtect ─────────────────────────────
    csrf = CSRFProtect()
    csrf.init_app(app)

    # ─── Session / Remember-me cookie security ──────────────────────────
    app.config.update({
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "REMEMBER_COOKIE_HTTPONLY": True,
    })
    if env == 'production':
        app.config.update({
            "SESSION_COOKIE_SECURE": True,
            "REMEMBER_COOKIE_SECURE": True,
        })
        
    # init extensions
    db.init_app(app)
    bcrypt.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    login_manager.login_view = 'login'

    # register health (and metrics) routes
    from app.routes.health import health_bp
    app.register_blueprint(health_bp)


    socketio.init_app(
        app,
        async_mode='gevent',
        cors_allowed_origins="*",
        message_queue=app.config['REDIS_URL']
    )

    # (optional) register blueprints here...

    return app
