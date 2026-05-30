import logging
import threading
from datetime import date
from decimal import Decimal

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, url_for)

from auth import require_role
from db import get_db
from models import (AdminRole, AdminRoleAssignment, Category, D4HHours, D4HMember,
                    HourType, HoursRecord, RecordStatus, User, UserRole)

logger = logging.getLogger(__name__)
admin_bp = Blueprint('admin', __name__)


@admin_bp.route('/admin')
@require_role('admin')
def index():
    db = get_db()
    from sqlalchemy import func
    last_sync = db.query(func.max(D4HMember.last_synced_at)).scalar()
    return render_template('admin/index.html',
        pending=db.query(HoursRecord).filter_by(status=RecordStatus.pending).count(),
        total_records=db.query(HoursRecord).count(),
        total_users=db.query(User).filter_by(is_active=True).count(),
        total_categories=db.query(Category).filter_by(is_active=True).count(),
        total_d4h_members=db.query(D4HMember).filter(
            D4HMember.status != 'Retired').count(),
        last_sync=last_sync,
        d4h_needs_resync_count=db.query(HoursRecord).filter_by(
            d4h_needs_resync=True).count(),
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
    if record.status == RecordStatus.submitted:
        try:
            from d4h_submit import handle_submitted_record_delete
            handle_submitted_record_delete(db, current_app.config['D4H_CONFIG'], record)
        except Exception as e:
            logger.warning(f'D4H retract on delete failed: {e}')
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


# ── Settings ─────────────────────────────────────────────────────────────────

@admin_bp.route('/admin/settings', methods=['GET', 'POST'])
@require_role('admin')
def admin_settings():
    import settings as s
    db = get_db()
    if request.method == 'POST':
        s.set_value(db, 'tax_credit_min_total',   request.form.get('tax_credit_min_total', '200'))
        s.set_value(db, 'tax_credit_min_primary',  request.form.get('tax_credit_min_primary', '101'))
        flash('Settings saved.')
        return redirect(url_for('admin.admin_settings'))
    es = s.get_eligibility_settings(db)
    return render_template('admin/settings.html', es=es)


# ── D4H Submit ───────────────────────────────────────────────────────────────

_submit_status: dict = {'running': False, 'result': None, 'error': None,
                        'message': '', 'percent': 0}
_submit_lock = __import__('threading').Lock()


@admin_bp.route('/admin/d4h-submit', methods=['POST'])
@require_role('admin')
def d4h_submit():
    global _submit_status
    with _submit_lock:
        if _submit_status.get('running'):
            flash('Submission already running.')
            return redirect(url_for('admin.index'))
        _submit_status = {'running': True, 'result': None, 'error': None,
                          'message': 'Starting…', 'percent': 0}

    import threading
    config = current_app.config['D4H_CONFIG']

    def _run():
        from db import _Session
        from d4h_submit import run_submission
        db = _Session()
        try:
            result = run_submission(db, config)
            _submit_status.update({
                'running': False, 'result': result, 'error': None,
                'message': 'Complete', 'percent': 100,
            })
        except Exception as e:
            logger.exception('Manual D4H submit failed')
            _submit_status.update({
                'running': False, 'result': None, 'error': str(e),
                'message': str(e), 'percent': 0,
            })
        finally:
            db.close()

    threading.Thread(target=_run, daemon=True, name='d4h-submit').start()
    return redirect(url_for('admin.index'))


@admin_bp.route('/admin/d4h-submit/status')
@require_role('admin')
def d4h_submit_status():
    from flask import jsonify
    return jsonify(_submit_status)


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


@admin_bp.route('/admin/categories/<int:cat_id>/delete', methods=['POST'])
@require_role('admin')
def delete_category(cat_id):
    db = get_db()
    cat = db.get(Category, cat_id)
    if not cat:
        abort(404)
    if cat.is_active:
        flash('Deactivate the category before deleting.', 'error')
        return redirect(url_for('admin.categories'))
    in_use = db.query(HoursRecord).filter_by(category_id=cat_id).count()
    if in_use:
        flash(f'Cannot delete — {in_use} record(s) use this category.', 'error')
        return redirect(url_for('admin.categories'))
    db.delete(cat)
    db.commit()
    flash(f'Category "{cat.name}" deleted.')
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


# ── Member Detail ─────────────────────────────────────────────────────────────

@admin_bp.route('/admin/members/<int:member_id>')
@require_role('admin')
def member_detail(member_id):
    db = get_db()
    member = db.get(D4HMember, member_id)
    if not member:
        abort(404)

    year = int(request.args.get('year', date.today().year))
    years = list(range(2026, date.today().year + 1))

    d4h_hours = (db.query(D4HHours)
                 .filter_by(d4h_member_id=member_id)
                 .order_by(D4HHours.date.desc())
                 .all())

    tool_records = []
    if member.user:
        tool_records = (db.query(HoursRecord)
                        .filter_by(user_id=member.user.id)
                        .order_by(HoursRecord.date.desc())
                        .all())

    # Year summary
    from d4h_sync import hours_by_year
    summary = hours_by_year(d4h_hours, tool_records, year)

    records = []
    for h in d4h_hours:
        records.append({
            'source': 'd4h',
            'date': h.date,
            'name': h.activity_name or 'Unknown Activity',
            'sub': h.activity_type.capitalize() if h.activity_type else '—',
            'hour_type': h.hour_type.value,
            'hours': float(h.hours),
            'status': None,
            'search': f"{(h.activity_name or '').lower()} {(h.activity_type or '').lower()}",
        })
    for r in tool_records:
        records.append({
            'source': 'hl',
            'date': r.date,
            'name': r.category.name if r.category else '—',
            'sub': r.description[:60] if r.description else '',
            'hour_type': r.category.hour_type.value if r.category else 'none',
            'hours': float(r.hours),
            'status': r.status.value,
            'search': f"{(r.category.name if r.category else '').lower()} {(r.description or '').lower()}",
        })
    records.sort(key=lambda x: x['date'], reverse=True)

    from settings import get_eligibility_settings, check_eligibility
    es = get_eligibility_settings(db)
    hours_ok, primary_ok = check_eligibility(summary, es)

    return render_template('admin/member_detail.html',
                           member=member,
                           records=records,
                           summary=summary,
                           year=year, years=years,
                           es=es, hours_ok=hours_ok, primary_ok=primary_ok)


# ── Admin Record Edit ─────────────────────────────────────────────────────────

@admin_bp.route('/admin/records/<int:record_id>/edit', methods=['GET', 'POST'])
@require_role('admin')
def edit_record(record_id):
    from datetime import datetime as dt
    from models import RecordHistory
    db = get_db()
    record = db.get(HoursRecord, record_id)
    if not record:
        abort(404)
    categories = db.query(Category).filter_by(is_active=True).order_by(Category.name).all()

    if request.method == 'POST':
        before = {
            'date': str(record.date), 'hours': str(record.hours),
            'description': record.description, 'category_id': record.category_id,
            'status': record.status.value,
        }
        from flask import session as flask_session
        record.category_id = int(request.form['category_id'])
        record.date = date.fromisoformat(request.form['date'])
        record.hours = float(request.form['hours'])
        record.description = request.form.get('description', '').strip() or None
        new_status = request.form.get('status')
        if new_status and new_status in RecordStatus.__members__:
            record.status = RecordStatus(new_status)
        record.updated_at = dt.now()
        db.add(RecordHistory(
            record_id=record.id,
            action='admin_edit',
            performed_by=flask_session['user_id'],
            changes={'before': before, 'after': {
                'date': str(record.date), 'hours': str(record.hours),
                'description': record.description, 'category_id': record.category_id,
                'status': record.status.value,
            }},
        ))
        db.commit()

        if record.status == RecordStatus.submitted:
            try:
                from d4h_submit import push_group_immediately
                ok = push_group_immediately(
                    db, current_app.config['D4H_CONFIG'],
                    record.user_id,
                    record.category.hour_type.value if record.category else None,
                    record.date.year, record.date.month,
                )
                if not ok:
                    flash('Record updated — D4H sync failed, will retry automatically.',
                          'warning')
                    return redirect(url_for('admin.records'))
            except Exception as e:
                logger.warning(f'Immediate D4H push failed: {e}')

        flash('Record updated.')
        return redirect(url_for('admin.records'))

    return render_template('admin/edit_record.html', record=record,
                           categories=categories, statuses=RecordStatus)


# ── Admin Roles ───────────────────────────────────────────────────────────────

@admin_bp.route('/admin/roles')
@require_role('admin')
def roles():
    db = get_db()
    all_roles = db.query(AdminRole).order_by(AdminRole.name).all()
    categories = db.query(Category).filter_by(is_active=True).order_by(Category.name).all()
    users = db.query(User).filter_by(is_active=True).order_by(User.display_name).all()
    return render_template('admin/roles.html', roles=all_roles,
                           categories=categories, users=users,
                           today=date.today().isoformat())


@admin_bp.route('/admin/roles/new', methods=['POST'])
@require_role('admin')
def new_role():
    db = get_db()
    name = request.form.get('name', '').strip()
    if not name:
        flash('Name is required.', 'error')
        return redirect(url_for('admin.roles'))
    db.add(AdminRole(
        name=name,
        monthly_hours=float(request.form['monthly_hours']),
        category_id=int(request.form['category_id']),
        description=request.form.get('description', '').strip() or None,
    ))
    db.commit()
    flash(f'Role "{name}" created.')
    return redirect(url_for('admin.roles'))


@admin_bp.route('/admin/roles/<int:role_id>/toggle', methods=['POST'])
@require_role('admin')
def toggle_role(role_id):
    db = get_db()
    role = db.get(AdminRole, role_id)
    if not role:
        abort(404)
    role.is_active = not role.is_active
    db.commit()
    flash(f'Role {"activated" if role.is_active else "deactivated"}.')
    return redirect(url_for('admin.roles'))


@admin_bp.route('/admin/roles/<int:role_id>/delete', methods=['POST'])
@require_role('admin')
def delete_role(role_id):
    db = get_db()
    role = db.get(AdminRole, role_id)
    if not role:
        abort(404)
    if role.is_active:
        flash('Deactivate the role before deleting.', 'error')
        return redirect(url_for('admin.roles'))
    db.delete(role)
    db.commit()
    flash(f'Role "{role.name}" deleted.')
    return redirect(url_for('admin.roles'))


@admin_bp.route('/admin/roles/<int:role_id>/assign', methods=['POST'])
@require_role('admin')
def assign_role(role_id):
    db = get_db()
    role = db.get(AdminRole, role_id)
    if not role:
        abort(404)
    user_id = int(request.form['user_id'])
    start = date.fromisoformat(request.form.get('start_date') or str(date.today()))
    end_str = request.form.get('end_date', '').strip()
    end = date.fromisoformat(end_str) if end_str else None
    existing = db.query(AdminRoleAssignment).filter_by(
        user_id=user_id, admin_role_id=role_id).first()
    if existing:
        flash('User already assigned to this role.')
        return redirect(url_for('admin.roles'))
    db.add(AdminRoleAssignment(
        user_id=user_id, admin_role_id=role_id,
        start_date=start, end_date=end,
    ))
    db.commit()
    flash('User assigned.')
    return redirect(url_for('admin.roles'))


@admin_bp.route('/admin/roles/assignments/<int:assignment_id>/remove', methods=['POST'])
@require_role('admin')
def remove_assignment(assignment_id):
    db = get_db()
    a = db.get(AdminRoleAssignment, assignment_id)
    if not a:
        abort(404)
    db.delete(a)
    db.commit()
    flash('Assignment removed.')
    return redirect(url_for('admin.roles'))


@admin_bp.route('/admin/roles/<int:role_id>/generate', methods=['POST'])
@require_role('admin')
def generate_role_hours(role_id):
    """Generate records from each assignment's start_date up to today, backfilling any missing months."""
    import calendar
    from datetime import datetime as dt
    from sqlalchemy import extract
    from models import HoursRecord, RecordStatus, AdminRoleAssignment

    db = get_db()
    today = date.today()
    assignments = [a for a in db.query(AdminRoleAssignment).filter_by(
        admin_role_id=role_id).all()
        if a.start_date <= today]

    generated = 0
    for a in assignments:
        role = a.admin_role
        # Walk every month from start_date up to (but not including) the current month
        cur_year, cur_month = a.start_date.year, a.start_date.month
        while (cur_year, cur_month) < (today.year, today.month):
            existing = db.query(HoursRecord).filter(
                HoursRecord.auto_role_assignment_id == a.id,
                extract('year',  HoursRecord.date) == cur_year,
                extract('month', HoursRecord.date) == cur_month,
            ).first()
            if not existing:
                last_day = calendar.monthrange(cur_year, cur_month)[1]
                record_date = date(cur_year, cur_month, last_day)

                # Only generate if within the assignment's active period
                if a.end_date is None or a.end_date >= record_date:
                    from role_hours import pro_rated_hours
                    hours = pro_rated_hours(role.monthly_hours, cur_year, cur_month,
                                           a.start_date, a.end_date)
                    db.add(HoursRecord(
                        user_id=a.user_id,
                        category_id=role.category_id,
                        date=record_date,
                        hours=hours,
                        description=f'Auto: {role.name}',
                        status=RecordStatus.approved,
                        approved_at=dt.now(),
                        auto_role_assignment_id=a.id,
                    ))
                    generated += 1

            # Advance to next month
            if cur_month == 12:
                cur_year += 1
                cur_month = 1
            else:
                cur_month += 1

    db.commit()
    flash(f'Generated {generated} record{"s" if generated != 1 else ""} for past months.')
    return redirect(url_for('admin.roles'))
