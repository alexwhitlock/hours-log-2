import logging
import logging.handlers
import os

from flask import Flask, render_template, session
from werkzeug.middleware.proxy_fix import ProxyFix

# ── Logging ───────────────────────────────────────────────────────────────────
# Logs to stdout AND a rotating file in /app/logs/

LOG_DIR = os.environ.get('LOG_DIR', os.path.join(os.path.dirname(__file__), 'logs'))
os.makedirs(LOG_DIR, exist_ok=True)

_fmt = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')

_file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, 'hours_log.log'),
    maxBytes=5 * 1024 * 1024,  # 5 MB
    backupCount=3,
)
_file_handler.setFormatter(_fmt)

_sync_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, 'd4h_sync.log'),
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=5,
)
_sync_handler.setFormatter(_fmt)

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_fmt)

# Root logger → stdout + hours_log.log
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(_stream_handler)
root_logger.addHandler(_file_handler)

# d4h_sync logger also gets its own file at DEBUG level
sync_logger = logging.getLogger('d4h_sync')
sync_logger.setLevel(logging.DEBUG)
sync_logger.addHandler(_sync_handler)

# d4h_client DEBUG goes to the same sync log
client_logger = logging.getLogger('d4h_client')
client_logger.setLevel(logging.DEBUG)
client_logger.addHandler(_sync_handler)

logger = logging.getLogger(__name__)


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> Flask:
    from config import load_config
    config = load_config()

    app = Flask(__name__, template_folder='templates', static_folder='static')
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.secret_key = config['SECRET_KEY']
    app.config.update({
        'ALLOWED_DOMAIN': config.get('ALLOWED_DOMAIN', 'sbo-ovsar.ca'),
        'D4H_CONFIG': {
            'D4H_API_TOKEN': config['D4H_API_TOKEN'],
            'D4H_TEAM_ID': config['D4H_TEAM_ID'],
            'D4H_GOOGLE_FIELD_ID': config.get('D4H_GOOGLE_FIELD_ID', '1817'),
        },
    })

    # SQLite
    db_path = os.environ.get('DB_PATH', os.path.join(os.path.dirname(__file__), 'data', 'hours_log.db'))
    database_url = f'sqlite:///{db_path}'
    logger.info(f'Database: {database_url}')

    from db import init_db, close_db, get_engine
    init_db(database_url)
    app.teardown_appcontext(close_db)

    # Create all tables (no Alembic needed for SQLite)
    from models import Base
    Base.metadata.create_all(get_engine())
    logger.info('Database tables ready.')

    # Google OAuth
    from auth import oauth
    oauth.init_app(app)
    oauth.register(
        name='google',
        client_id=config['GOOGLE_CLIENT_ID'],
        client_secret=config['GOOGLE_CLIENT_SECRET'],
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={
            'scope': 'openid email profile',
            'hd': config.get('ALLOWED_DOMAIN', 'sbo-ovsar.ca'),
        },
    )

    # Blueprints
    from auth import auth_bp
    from routes.hours import hours_bp
    from routes.approvals import approvals_bp
    from routes.admin import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(hours_bp)
    app.register_blueprint(approvals_bp)
    app.register_blueprint(admin_bp)

    @app.context_processor
    def inject_user():
        return {
            'current_role': session.get('role', ''),
            'current_name': session.get('display_name', ''),
            'current_email': session.get('email', ''),
        }

    @app.errorhandler(403)
    def forbidden(e):
        return render_template('error.html', code=403, message=str(e.description)), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template('error.html', code=404, message='Page not found.'), 404

    @app.route('/health')
    def health():
        from flask import jsonify
        return jsonify({'status': 'ok'})

    logger.info('hours-log ready.')
    return app


app = create_app()
