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

    from mail import init_mail
    init_mail(config)
    from models import set_email_domain
    set_email_domain(config.get('ALLOWED_DOMAIN', 'sbo-ovsar.ca'))

    # Create all tables (no Alembic needed for SQLite)
    from models import Base
    Base.metadata.create_all(get_engine())
    from db import run_migrations
    run_migrations()
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

    @app.template_filter('status_label')
    def status_label(value):
        return 'Pushed to D4H' if value == 'submitted' else value.capitalize()

    _ACTION_LABELS = {
        'created':      'Saved as draft',
        'submitted':    'Submitted for approval',
        'resubmitted':  'Edited & resubmitted',
        'edited':       'Saved as draft',
        'approved':     'Approved',
        'rejected':     'Rejected',
        'pushed_to_d4h': 'Pushed to D4H',
    }

    @app.template_filter('action_label')
    def action_label(value):
        return _ACTION_LABELS.get(value, value.replace('_', ' ').capitalize())

    @app.context_processor
    def inject_user():
        return {
            'current_role': session.get('role', ''),
            'current_name': session.get('display_name', ''),
            'current_email': session.get('email', '') or '',
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

    _start_weekly_scheduler(app)

    logger.info('hours-log ready.')
    return app


def _start_weekly_scheduler(app: 'Flask') -> None:
    import threading

    def _run():
        import time
        from datetime import datetime as _dt
        last_submission_date = None

        while True:
            time.sleep(3600)  # check every hour
            try:
                _send_summaries(app)
                _run_role_hours()
                # 3am jobs
                now = _dt.now()
                if now.hour == 3 and last_submission_date != now.date():
                    _run_nightly_sync_and_submit(app.config['D4H_CONFIG'])
                    last_submission_date = now.date()
                    # Monthly progress summary on the 1st of each month
                    if now.day == 1:
                        _send_monthly_progress_summaries(app)
            except Exception:
                logger.exception('Scheduler error')

    t = threading.Thread(target=_run, daemon=True, name='summary-scheduler')
    t.start()
    logger.info('Summary scheduler started.')


def _run_nightly_sync_and_submit(config: dict) -> None:
    from db import _Session
    from d4h_sync import sync_all
    from d4h_submit import run_submission
    db = _Session()
    try:
        logger.info('Nightly sync: starting D4H pull…')
        sync_all(config, db)
        logger.info('Nightly sync: D4H pull complete, starting submission…')
        result = run_submission(db, config)
        logger.info(f'Nightly sync & submit complete: {result}')
    except Exception:
        logger.exception('Nightly sync & submit error')
    finally:
        db.close()


def _send_monthly_progress_summaries(app) -> None:
    from datetime import datetime, timedelta
    from db import _Session
    from models import User, HoursRecord, RecordStatus, D4HHours
    from mail import send_monthly_progress
    from d4h_sync import hours_by_year

    # Summarise the previous month
    today = datetime.now().date()
    first_of_this_month = today.replace(day=1)
    last_month = first_of_this_month - timedelta(days=1)
    year, month = last_month.year, last_month.month
    month_name = last_month.strftime('%B %Y')

    db = _Session()
    try:
        users = db.query(User).filter(
            User.is_active == True,
            User.last_login_at != None,
            User.notify_monthly_summary == True,
        ).all()
        for user in users:
            d4h_hours = db.query(D4HHours).filter_by(
                user_id=user.id).all() if user.d4h_id else []
            # tool_records is now list of HoursRecord; hours_by_year reads via record.entry
            tool_records = db.query(HoursRecord).filter_by(user_id=user.id).all()
            summary = hours_by_year(d4h_hours, tool_records, year)
            send_monthly_progress(
                user.email, user.display_name, month_name,
                float(summary['tax_credit']),
                float(summary['primary']),
                float(summary['secondary']),
                float(summary['other']),
                float(summary['total']),
            )
        if users:
            logger.info(f'Monthly progress summaries sent to {len(users)} users')
    finally:
        db.close()


def _run_role_hours() -> None:
    from db import _Session
    from role_hours import generate_monthly_role_hours
    db = _Session()
    try:
        n = generate_monthly_role_hours(db)
        if n:
            logger.info(f'Role hours: auto-generated {n} records')
    finally:
        db.close()


def _send_summaries(app: 'Flask') -> None:
    from datetime import datetime, timedelta
    from db import _Session
    from models import User, NotifyPref, HoursRecord, RecordStatus, D4HHours
    from mail import send_weekly_summary
    from d4h_sync import hours_by_year

    db = _Session()
    try:
        users = db.query(User).filter(
            User.is_active == True,
            User.last_login_at != None,
            User.notify_approval.in_([NotifyPref.daily, NotifyPref.weekly]),
        ).all()
        year = datetime.now().year
        sent = 0
        for user in users:
            interval = timedelta(days=1 if user.notify_approval == NotifyPref.daily else 7)
            if user.last_weekly_sent and user.last_weekly_sent > datetime.now() - interval:
                continue
            tool_records = db.query(HoursRecord).filter_by(user_id=user.id).all()
            d4h_hours = db.query(D4HHours).filter_by(
                user_id=user.id).all() if user.d4h_id else []
            summary = hours_by_year(d4h_hours, tool_records, year)
            pending = sum(
                1 for r in tool_records
                if r.entry and r.entry.status == RecordStatus.pending
            )
            approved_recent = sum(
                1 for r in tool_records
                if r.entry and r.entry.status == RecordStatus.approved
                and r.entry.approved_at and r.entry.approved_at > datetime.now() - interval
            )
            if send_weekly_summary(user.email, user.display_name, pending,
                                   approved_recent, float(summary['tax_credit'])):
                user.last_weekly_sent = datetime.now()
                sent += 1
        db.commit()
        if sent:
            logger.info(f'Summary scheduler: sent {sent} emails')
    finally:
        db.close()


app = create_app()
