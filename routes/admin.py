import logging
import threading
from datetime import date
from decimal import Decimal

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, url_for)

from auth import require_role
from db import get_db
from models import (Category, D4HHours, D4HMember, HourType, HoursRecord,
                    RecordStatus, User, UserRole)

logger = logging.getLogger(__name__)
admin_bp = Blueprint('admin', __name__)


@admin_bp.route('/admin')
@require_role('admin')
def index():
    db = get_db()
    return render_template('admin/index.html',
        pending=db.query(HoursRecord).filter_by(status=RecordStatus.pending).count(),
        total_records=db.query(HoursRecord).count(),
        total_users=db.query(User).filter_by(is_active=True).count(),
        total_categories=db.query(Category).filter_by(is_active=True).count(),
        total_d4h_members=db.query(D4HMember).filter(
            D4HMember.status != 'Retired').count(),
    )


# ── All Records ───────────────────────────────────────────────────────────────

@admin_bp.route('/admin/records')
@require_role('admin')
def records():
    db = get_db()
    status_filter = request.args.get('status')
    q = db.query(HoursRecord)
    if status_filter and status_filter in RecordStatus.__members__:
        q = q.filter(HoursRecord.status == RecordStatus(status_filter))
    records = q.order_by(HoursRecord.date.desc()).limit(500).all()
    return render_template('admin/records.html', records=records,
                           status_filter=status_filter, statuses=RecordStatus)


@admin_bp.route('/admin/records/<int:record_id>/delete', methods=['POST'])
@require_role('admin')
def delete_record(record_id):
    db = get_db()
    record = db.get(HoursRecord, record_id)
    if not record:
        abort(404)
    from models import RecordHistory
    db.query(RecordHistory).filter_by(record_id=record_id).delete()
    db.delete(record)
    db.commit()
    flash('Record deleted.')
    return redirect(url_for('admin.records'))


@admin_bp.route('/admin/records/<int:record_id>/history')
@require_role('admin')
def record_history(record_id):
    db = get_db()
    record = db.get(HoursRecord, record_id)
    if not record:
        abort(404)
    return render_template('admin/history.html', record=record)


# ── Categories ────────────────────────────────────────────────────────────────

@admin_bp.route('/admin/categories')
@require_role('admin')
def categories():
    db = get_db()
    cats = db.query(Category).order_by(Category.name).all()
    return render_template('admin/categories.html', categories=cats, hour_types=HourType)


@admin_bp.route('/admin/categories/new', methods=['POST'])
@require_role('admin')
def new_category():
    db = get_db()
    name = request.form.get('name', '').strip()
    if not name:
        flash('Category name is required.', 'error')
        return redirect(url_for('admin.categories'))
    tag_id = request.form.get('d4h_tag_id', '').strip()
    hour_type = request.form.get('hour_type', 'none')
    db.add(Category(name=name, d4h_tag_id=int(tag_id) if tag_id else None,
                    hour_type=HourType(hour_type)))
    db.commit()
    flash(f'Category "{name}" created.')
    return redirect(url_for('admin.categories'))


@admin_bp.route('/admin/categories/<int:cat_id>/toggle', methods=['POST'])
@require_role('admin')
def toggle_category(cat_id):
    db = get_db()
    cat = db.get(Category, cat_id)
    if not cat:
        abort(404)
    cat.is_active = not cat.is_active
    db.commit()
    flash(f'Category {"activated" if cat.is_active else "deactivated"}.')
    return redirect(url_for('admin.categories'))


# ── Users ─────────────────────────────────────────────────────────────────────

@admin_bp.route('/admin/users')
@require_role('admin')
def users():
    db = get_db()
    return render_template('admin/users.html',
                           users=db.query(User).order_by(User.display_name).all(),
                           roles=UserRole)


@admin_bp.route('/admin/users/<int:user_id>/role', methods=['POST'])
@require_role('admin')
def set_role(user_id):
    db = get_db()
    user = db.get(User, user_id)
    if not user:
        abort(404)
    user.role = UserRole(request.form['role'])
    db.commit()
    flash(f'Role updated for {user.display_name}.')
    return redirect(url_for('admin.users'))


@admin_bp.route('/admin/users/<int:user_id>/toggle', methods=['POST'])
@require_role('admin')
def toggle_user(user_id):
    db = get_db()
    user = db.get(User, user_id)
    if not user:
        abort(404)
    user.is_active = not user.is_active
    db.commit()
    flash(f'{"Activated" if user.is_active else "Deactivated"} {user.display_name}.')
    return redirect(url_for('admin.users'))


# ── D4H Sync ──────────────────────────────────────────────────────────────────

_sync_status = {
    'running': False, 'result': None, 'error': None,
    'phase': '', 'message': '', 'percent': 0,
}


def _progress(phase: str, message: str, percent: int) -> None:
    _sync_status.update({'phase': phase, 'message': message,
                         'percent': max(0, min(100, percent))})
    logger.info(f'Sync [{phase}] {percent}%: {message}')


def _run_sync_background(config: dict) -> None:
    from db import _Session
    from d4h_sync import sync_all
    db = _Session()
    try:
        result = sync_all(config, db, progress=_progress)
        _sync_status.update({
            'running': False, 'result': result, 'error': None,
            'phase': 'done', 'message': 'Sync complete', 'percent': 100,
        })
        logger.info(f'Sync complete: {result}')
    except Exception as e:
        logger.exception('Sync failed')
        _sync_status.update({
            'running': False, 'result': None, 'error': str(e),
            'phase': 'error', 'message': str(e), 'percent': 0,
        })
    finally:
        db.close()


@admin_bp.route('/admin/sync', methods=['POST'])
@require_role('admin')
def sync():
    if _sync_status['running']:
        flash('A sync is already in progress.', 'warn')
        return redirect(url_for('admin.index'))
    config = current_app.config.get('D4H_CONFIG') or {}
    if not config.get('D4H_API_TOKEN'):
        flash('D4H credentials not configured.', 'error')
        return redirect(url_for('admin.index'))
    _sync_status.update({
        'running': True, 'result': None, 'error': None,
        'phase': 'starting', 'message': 'Starting…', 'percent': 0,
    })
    t = threading.Thread(target=_run_sync_background, args=(config,), daemon=True)
    t.start()
    return redirect(url_for('admin.index'))


@admin_bp.route('/admin/sync/status')
@require_role('admin')
def sync_status():
    return jsonify(_sync_status)


# ── Hours Report ──────────────────────────────────────────────────────────────

@admin_bp.route('/admin/hours-report')
@require_role('admin')
def hours_report():
    db = get_db()
    year = int(request.args.get('year', date.today().year))

    members = db.query(D4HMember).filter(
        D4HMember.status != 'Retired').order_by(D4HMember.name).all()

    tool_records = db.query(HoursRecord).filter(
        HoursRecord.status.in_([RecordStatus.approved, RecordStatus.submitted])).all()

    tool_by_member = {}
    for r in tool_records:
        if r.date.year != year:
            continue
        mid = r.user.d4h_member_id if r.user else None
        if mid:
            tool_by_member.setdefault(mid, []).append(r)

    d4h_by_member = {}
    for h in db.query(D4HHours).filter(
        D4HHours.date >= date(year, 1, 1),
        D4HHours.date <= date(year, 12, 31),
    ).all():
        d4h_by_member.setdefault(h.d4h_member_id, []).append(h)

    rows = []
    for m in members:
        d4h_hrs = d4h_by_member.get(m.id, [])
        tool_hrs = tool_by_member.get(m.id, [])

        def _d4h(types):
            return sum((float(h.hours) for h in d4h_hrs if h.hour_type.value in types), 0.0)

        def _tool(types):
            return sum((float(r.hours) for r in tool_hrs
                        if r.category and r.category.hour_type.value in types), 0.0)

        p_d4h  = _d4h(('primary',))
        p_tool = _tool(('primary',))
        s_d4h  = _d4h(('secondary',))
        s_tool = _tool(('secondary',))
        o_d4h  = _d4h(('other', 'none'))
        o_tool = _tool(('other', 'none'))
        tax_credit = p_d4h + p_tool + s_d4h + s_tool

        rows.append({
            'member': m,
            'has_login': m.user is not None and m.user.is_active,
            'p_d4h': p_d4h, 'p_tool': p_tool,
            's_d4h': s_d4h, 's_tool': s_tool,
            'o_d4h': o_d4h, 'o_tool': o_tool,
            'tax_credit': tax_credit,
            'total': tax_credit + o_d4h + o_tool,
            # convenience totals for display
            'primary': p_d4h + p_tool,
            'secondary': s_d4h + s_tool,
            'other': o_d4h + o_tool,
        })

    years = list(range(2026, date.today().year + 1))
    return render_template('admin/hours_report.html',
                           rows=rows, year=year, years=years)
