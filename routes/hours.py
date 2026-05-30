from datetime import date, datetime

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   session, url_for)

from auth import require_login
from db import get_db
from models import (Category, D4HHours, HoursRecord, RecordHistory,
                    RecordStatus, User, NotifyPref, UserRole)

hours_bp = Blueprint('hours', __name__)


@hours_bp.route('/')
@hours_bp.route('/hours')
@require_login
def index():
    db = get_db()
    show_rejected = request.args.get('show_rejected') == '1'
    q = db.query(HoursRecord).filter_by(user_id=session['user_id'])
    if not show_rejected:
        q = q.filter(HoursRecord.status != RecordStatus.rejected)
    records = q.order_by(HoursRecord.date.desc()).all()
    return render_template('hours/index.html', records=records, show_rejected=show_rejected)


@hours_bp.route('/hours/new', methods=['GET', 'POST'])
@require_login
def new():
    db = get_db()
    categories = db.query(Category).filter_by(is_active=True).order_by(Category.name).all()

    if request.method == 'POST':
        action = request.form.get('action', 'draft')
        record = HoursRecord(
            user_id=session['user_id'],
            category_id=int(request.form['category_id']),
            date=date.fromisoformat(request.form['date']),
            hours=float(request.form['hours']),
            description=request.form.get('description', '').strip() or None,
            status=RecordStatus.pending if action == 'submit' else RecordStatus.draft,
        )
        db.add(record)
        db.flush()
        db.add(RecordHistory(
            record_id=record.id,
            action='submitted' if action == 'submit' else 'created',
            performed_by=session['user_id'],
        ))
        db.commit()

        if action == 'submit':
            from mail import notify_pending_submitted
            approvers = db.query(User).filter(
                User.role.in_([UserRole.approver, UserRole.admin]),
                User.is_active == True,
                User.notify_pending == NotifyPref.realtime,
            ).all()
            submitter = db.get(User, session['user_id'])
            for approver in approvers:
                notify_pending_submitted(approver.email, approver.display_name,
                                         submitter.display_name, record)

        flash('Submitted for approval.' if action == 'submit' else 'Saved as draft.')
        return redirect(url_for('hours.index'))

    return render_template('hours/form.html', record=None, categories=categories,
                           today=date.today().isoformat())


@hours_bp.route('/hours/<int:record_id>/edit', methods=['GET', 'POST'])
@require_login
def edit(record_id):
    db = get_db()
    record = db.query(HoursRecord).filter_by(
        id=record_id, user_id=session['user_id']).first()
    if not record:
        abort(404)
    if record.status not in (RecordStatus.draft, RecordStatus.pending):
        flash('Only draft or pending records can be edited.')
        return redirect(url_for('hours.index'))

    categories = db.query(Category).filter_by(is_active=True).order_by(Category.name).all()

    if request.method == 'POST':
        action = request.form.get('action', 'draft')
        was_pending = record.status == RecordStatus.pending
        before = {
            'date': str(record.date), 'hours': str(record.hours),
            'description': record.description, 'category_id': record.category_id,
            'status': record.status.value,
        }
        record.category_id = int(request.form['category_id'])
        record.date = date.fromisoformat(request.form['date'])
        record.hours = float(request.form['hours'])
        record.description = request.form.get('description', '').strip() or None
        # Editing always moves back to draft; user must re-submit
        record.status = RecordStatus.pending if action == 'submit' else RecordStatus.draft
        record.updated_at = datetime.now()
        db.add(RecordHistory(
            record_id=record.id,
            action='submitted' if action == 'submit' else 'edited',
            performed_by=session['user_id'],
            changes={'before': before, 'after': {
                'date': str(record.date), 'hours': str(record.hours),
                'description': record.description, 'category_id': record.category_id,
                'status': record.status.value,
            }},
        ))
        db.commit()
        if action == 'submit':
            flash('Re-submitted for approval.')
        elif was_pending:
            flash('Record moved back to draft.')
        else:
            flash('Draft saved.')
        return redirect(url_for('hours.index'))

    return render_template('hours/form.html', record=record, categories=categories,
                           today=date.today().isoformat())


@hours_bp.route('/hours/<int:record_id>/delete', methods=['POST'])
@require_login
def delete(record_id):
    db = get_db()
    record = db.query(HoursRecord).filter_by(
        id=record_id, user_id=session['user_id']).first()
    if not record:
        abort(404)
    if record.status not in (RecordStatus.draft, RecordStatus.pending):
        flash('Only draft or pending records can be deleted.', 'error')
        return redirect(url_for('hours.index'))
    db.query(RecordHistory).filter_by(record_id=record_id).delete()
    db.delete(record)
    db.commit()
    flash('Record deleted.')
    return redirect(url_for('hours.index'))


@hours_bp.route('/hours/<int:record_id>/history')
@require_login
def history(record_id):
    db = get_db()
    record = db.query(HoursRecord).filter_by(
        id=record_id, user_id=session['user_id']).first()
    if not record:
        abort(404)
    return render_template('hours/history.html', record=record)


@hours_bp.route('/profile/notifications', methods=['POST'])
@require_login
def save_notifications():
    db = get_db()
    user = db.get(User, session['user_id'])
    pref = request.form.get('notify_approval', 'off')
    pref_pending = request.form.get('notify_pending', 'off')
    if pref in ('off', 'realtime', 'daily', 'weekly'):
        user.notify_approval = NotifyPref(pref)
    if pref_pending in ('off', 'realtime', 'daily', 'weekly'):
        user.notify_pending = NotifyPref(pref_pending)
    user.notify_monthly_summary = bool(request.form.get('notify_monthly_summary'))
    user.notify_tax_credit = bool(request.form.get('notify_tax_credit'))
    db.commit()
    flash('Notification preferences saved.')
    return redirect(url_for('hours.profile'))


@hours_bp.route('/profile')
@require_login
def profile():
    from datetime import date as date_cls
    from d4h_sync import hours_by_year

    db = get_db()
    user = db.get(User, session['user_id'])
    year = int(request.args.get('year', date_cls.today().year))

    tool_records = db.query(HoursRecord).filter_by(user_id=user.id).all()
    pending_count = sum(1 for r in tool_records if r.status == RecordStatus.pending)
    draft_count = sum(1 for r in tool_records if r.status == RecordStatus.draft)

    d4h_hours_list = []
    if user.d4h_member_id:
        d4h_hours_list = db.query(D4HHours).filter_by(
            d4h_member_id=user.d4h_member_id).all()

    summary = hours_by_year(d4h_hours_list, tool_records, year)
    years = list(range(2026, date_cls.today().year + 1))

    from settings import get_eligibility_settings, check_eligibility
    es = get_eligibility_settings(db)
    hours_ok, primary_ok = check_eligibility(summary, es)

    return render_template('profile.html', user=user,
                           summary=summary, year=year, years=years,
                           pending_count=pending_count,
                           draft_count=draft_count,
                           has_d4h=user.d4h_member_id is not None,
                           es=es, hours_ok=hours_ok, primary_ok=primary_ok)


@hours_bp.route('/attendance')
@require_login
def attendance():
    from datetime import date as date_cls
    db = get_db()
    user = db.get(User, session['user_id'])

    d4h_hours = []
    if user.d4h_member_id:
        d4h_hours = (db.query(D4HHours)
                     .filter_by(d4h_member_id=user.d4h_member_id)
                     .order_by(D4HHours.date.desc())
                     .all())

    tool_records = (db.query(HoursRecord)
                    .filter_by(user_id=user.id)
                    .order_by(HoursRecord.date.desc())
                    .all())

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

    return render_template('attendance.html', user=user, records=records)
