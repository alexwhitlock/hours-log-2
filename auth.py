import functools
import logging
from datetime import datetime

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, abort, redirect, request, session, url_for

logger = logging.getLogger(__name__)
oauth = OAuth()
auth_bp = Blueprint('auth', __name__)


def require_login(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            session['next'] = request.url
            return redirect(url_for('auth.login_page'))
        return f(*args, **kwargs)
    return decorated


def require_role(min_role: str):
    _order = {'member': 0, 'approver': 1, 'admin': 2}
    def decorator(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                session['next'] = request.url
                return redirect(url_for('auth.login_page'))
            if _order.get(session.get('role', ''), -1) < _order[min_role]:
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator


@auth_bp.route('/auth/google')
def login():
    redirect_uri = url_for('auth.callback', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route('/auth/callback')
def callback():
    from flask import current_app
    from db import get_db
    from models import User, UserRole

    token = oauth.google.authorize_access_token()
    info = token.get('userinfo') or oauth.google.userinfo()

    allowed_domain = current_app.config.get('ALLOWED_DOMAIN', 'sbo-ovsar.ca')
    email = (info.get('email') or '').lower()
    if not email.endswith(f'@{allowed_domain}'):
        abort(403, f'Access restricted to @{allowed_domain} accounts.')

    google_sub = info['sub']
    display_name = info.get('name', email)
    google_username = email.split('@')[0].lower()

    db = get_db()
    user = db.query(User).filter_by(google_sub=google_sub).first()

    if not user:
        # Find by google_username (pre-created from D4H sync)
        user = db.query(User).filter_by(google_username=google_username).first()
        if user:
            user.google_sub = google_sub

    if not user:
        user = User(
            google_sub=google_sub,
            google_username=google_username,
            display_name=display_name,
            role=UserRole.member,
        )
        db.add(user)
    else:
        if not user.is_active:
            abort(403, 'Your account has been deactivated.')
        user.google_username = google_username

    user.display_name = display_name
    user.last_login_at = datetime.now()
    db.commit()

    session.clear()
    session.permanent = True
    session['user_id'] = user.id
    session['role'] = user.role.value
    session['display_name'] = user.display_name

    next_url = session.pop('next', None)
    return redirect(next_url or url_for('hours.index'))


@auth_bp.route('/auth/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login_page'))


@auth_bp.route('/login')
def login_page():
    from flask import render_template
    return render_template('login.html')
