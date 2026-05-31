from datetime import date, datetime

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   session, url_for)

from auth import require_login
from db import get_db
from models import (Category, D4HHours, EntryHistory, HoursEntry, HoursRecord,
                    RecordStatus, User, NotifyPref, UserRole)

hours_bp = Blueprint('hours', __name__)


@hours_bp.route('/')
@hours_bp.route('/hours')
@require_login
def index():
    db = get_db()
    show_rejected = request.args.get('show_rejected') == '1'
    # Query HoursRecord for current user, then get distinct entries
    records_qs = db.query(HoursRecord).filter_by(user_id=session['user_id']).all()
    entry_ids = {r.entry_id for r in records_qs}
    q = db.query(HoursEntry).filter(HoursEntry.id.in_(entry_ids))
    if not show_rejected:
        q = q.filter(HoursEntry.status != RecordStatus.rejected)
    entries = q.order_by(HoursEntry.date.desc()).all()
    # Build a map of entry_id -> all records (for "for N people" display)
    all_records = db.query(HoursRecord).filter(HoursRecord.entry_id.in_(entry_ids)).all() if entry_ids else []
    entry_record_map = {}
    for r in all_records:
        entry_record_map.setdefault(r.entry_id, []).append(r)
    return render_template('hours/index.html', entries=entries,
                           entry_record_map=entry_record_map,
                           current_user_id=session['user_id'],
                           show_rejected=show_rejected)


@hours_bp.route('/hours/new', methods=['GET', 'POST'])
@require_login
def new():
    db = get_db()
    categories = db.query(Category).filter(Category.is_active==True, Category.is_system==False).order_by(Category.name).all()
    all_members = db.query(User).filter_by(is_active=True).order_by(User.display_name).all()

    if request.method == 'POST':
        action = request.form.get('action', 'draft')
        user_ids_raw = request.form.getlist('user_ids')
        # Deduplicate and validate user IDs
        user_ids = list(dict.fromkeys(
            int(uid) for uid in user_ids_raw if uid.strip().isdigit()
        ))
        if not user_ids:
            user_ids = [session['user_id']]

        entry = HoursEntry(
            submitted_by=session['user_id'],
            category_id=int(request.form['category_id']),
            date=date.fromisoformat(request.form['date']),
            hours=float(request.form['hours']),
            description=request.form.get('description', '').strip() or None,
            status=RecordStatus.pending if action == 'submit' else RecordStatus.draft,
        )
        db.add(entry)
        db.flush()

        for uid in user_ids:
            db.add(HoursRecord(entry_id=entry.id, user_id=uid))

        db.add(EntryHistory(
            entry_id=entry.id,
            action='submitted' if action == 'submit' else 'created',
            performed_by=session['user_id'],
        ))
        db.commit()

        if action == 'submit':
            from mail import notify_pending_submitted
            from models import CategoryApprover
            assigned_user_ids = {
                a.user_id for a in db.query(CategoryApprover)
                .filter_by(category_id=entry.category_id).all()
            }
            approvers = [
                u for u in db.query(User).filter(
                    User.is_active == True,
                    User.notify_pending == NotifyPref.realtime,
                ).all()
                if u.role == UserRole.admin or u.id in assigned_user_ids
            ]
            submitter = db.get(User, session['user_id'])
            member_names = [r.user.display_name for r in entry.records if r.user]
            for approver in approvers:
                notify_pending_submitted(approver.email, approver.display_name,
                                         submitter.display_name, entry, member_names)

        flash('Submitted for approval.' if action == 'submit' else 'Saved as draft.')
        return redirect(url_for('hours.index'))

    return render_template('hours/form.html', entry=None, categories=categories,
                           all_members=all_members,
                           current_user_id=session['user_id'],
                           today=date.today().isoformat())


@hours_bp.route('/hours/<int:entry_id>/edit', methods=['GET', 'POST'])
@require_login
def edit(entry_id):
    db = get_db()
    # Verify current user has a record in this entry
    record = db.query(HoursRecord).filter_by(
        entry_id=entry_id, user_id=session['user_id']).first()
    if not record:
        abort(404)
    entry = record.entry
    if not entry or entry.submitted_by != session['user_id']:
        abort(404)
    if entry.status not in (RecordStatus.draft, RecordStatus.pending):
        flash('Only draft or pending entries can be edited.')
        return redirect(url_for('hours.index'))

    categories = db.query(Category).filter(Category.is_active==True, Category.is_system==False).order_by(Category.name).all()
    all_members = db.query(User).filter_by(is_active=True).order_by(User.display_name).all()

    if request.method == 'POST':
        action = request.form.get('action', 'draft')
        was_pending = entry.status == RecordStatus.pending
        before = {
            'date': str(entry.date), 'hours': str(entry.hours),
            'description': entry.description, 'category_id': entry.category_id,
            'status': entry.status.value,
        }
        entry.category_id = int(request.form['category_id'])
        entry.date = date.fromisoformat(request.form['date'])
        entry.hours = float(request.form['hours'])
        entry.description = request.form.get('description', '').strip() or None
        entry.status = RecordStatus.pending if action == 'submit' else RecordStatus.draft
        entry.updated_at = datetime.now()

        db.add(EntryHistory(
            entry_id=entry.id,
            action='submitted' if action == 'submit' else 'edited',
            performed_by=session['user_id'],
            changes={'before': before, 'after': {
                'date': str(entry.date), 'hours': str(entry.hours),
                'description': entry.description, 'category_id': entry.category_id,
                'status': entry.status.value,
            }},
        ))
        db.commit()
        if action == 'submit':
            flash('Re-submitted for approval.')
        elif was_pending:
            flash('Entry moved back to draft.')
        else:
            flash('Draft saved.')
        return redirect(url_for('hours.index'))

    return render_template('hours/form.html', entry=entry, categories=categories,
                           all_members=all_members,
                           current_user_id=session['user_id'],
                           today=date.today().isoformat())


@hours_bp.route('/hours/<int:entry_id>/delete', methods=['POST'])
@require_login
def delete(entry_id):
    db = get_db()
    record = db.query(HoursRecord).filter_by(
        entry_id=entry_id, user_id=session['user_id']).first()
    if not record:
        abort(404)
    entry = record.entry
    if not entry:
        abort(404)
    if entry.status not in (RecordStatus.draft, RecordStatus.pending):
        flash('Only draft or pending entries can be deleted.', 'error')
        return redirect(url_for('hours.index'))

    # If this is the only record in the entry, delete the entry (cascades)
    if len(entry.records) <= 1:
        db.delete(entry)
    else:
        db.delete(record)
    db.commit()
    flash('Record deleted.')
    return redirect(url_for('hours.index'))


@hours_bp.route('/hours/<int:entry_id>/history')
@require_login
def history(entry_id):
    db = get_db()
    record = db.query(HoursRecord).filter_by(
        entry_id=entry_id, user_id=session['user_id']).first()
    if not record:
        abort(404)
    entry = record.entry
    if not entry:
        abort(404)
    return render_template('hours/history.html', entry=entry)


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
    pending_count = sum(1 for r in tool_records if r.entry and r.entry.status == RecordStatus.pending)
    draft_count = sum(1 for r in tool_records if r.entry and r.entry.status == RecordStatus.draft)

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
        e = r.entry
        if not e:
            continue
        records.append({
            'source': 'hl',
            'date': e.date,
            'name': e.category.name if e.category else '—',
            'sub': e.description[:60] if e.description else '',
            'hour_type': e.category.hour_type.value if e.category else 'none',
            'hours': float(e.hours),
            'status': e.status.value,
            'search': f"{(e.category.name if e.category else '').lower()} {(e.description or '').lower()}",
        })
    records.sort(key=lambda x: x['date'], reverse=True)

    return render_template('attendance.html', user=user, records=records)
