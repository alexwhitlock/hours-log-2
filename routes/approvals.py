from datetime import datetime, date

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   session, url_for)

from auth import require_role
from db import get_db
from models import (Category, CategoryApprover, D4HHours, EntryHistory,
                    HoursEntry, HoursRecord, RecordStatus, NotifyPref, User, UserRole)

approvals_bp = Blueprint('approvals', __name__)


def _push_to_d4h(db, record) -> None:
    """Immediately push the approved record's group to D4H. Failures are flagged for retry.
    record is a HoursRecord; metadata comes from record.entry."""
    entry = record.entry
    if not entry:
        return
    if not record.user or not record.user.d4h_member_id:
        return
    if not entry.category or entry.category.hour_type.value not in ('primary', 'secondary', 'other'):
        return
    try:
        from flask import current_app
        from d4h_submit import push_group_immediately
        push_group_immediately(
            db, current_app.config['D4H_CONFIG'],
            record.user_id,
            entry.category.hour_type.value,
            entry.date.year, entry.date.month,
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f'D4H push on approval failed: {e}')


def _check_tax_credit_milestone(db, user):
    """Send tax credit eligibility email if user just became eligible this year."""
    if not user or not user.notify_tax_credit:
        return
    year = datetime.now().year
    if user.tax_credit_notified_year == year:
        return  # already notified this year

    from d4h_sync import hours_by_year
    from settings import get_eligibility_settings, check_eligibility
    d4h_hours = db.query(D4HHours).filter_by(d4h_member_id=user.d4h_member_id).all() \
        if user.d4h_member_id else []
    tool_records = db.query(HoursRecord).filter_by(user_id=user.id).all()
    summary = hours_by_year(d4h_hours, tool_records, year)
    es = get_eligibility_settings(db)
    hours_ok, primary_ok = check_eligibility(summary, es)
    if hours_ok and primary_ok:
        from mail import send_tax_credit_eligible
        send_tax_credit_eligible(user.email, user.display_name,
                                 float(summary['total']), year)
        user.tax_credit_notified_year = year
        db.commit()


@approvals_bp.route('/approvals')
@require_role('approver')
def index():
    db = get_db()
    base_q = db.query(HoursEntry).filter_by(status=RecordStatus.pending)

    if session.get('role') != 'admin':
        assigned = {a.category_id for a in db.query(CategoryApprover)
                    .filter_by(user_id=session['user_id']).all()}
        base_q = base_q.filter(HoursEntry.category_id.in_(assigned))

    entries = base_q.order_by(HoursEntry.date.desc()).all()
    return render_template('approvals/index.html', entries=entries)


@approvals_bp.route('/approvals/<int:entry_id>/review', methods=['GET', 'POST'])
@require_role('approver')
def review(entry_id):
    db = get_db()
    entry = db.query(HoursEntry).filter_by(
        id=entry_id, status=RecordStatus.pending).first()
    if not entry:
        abort(404)

    categories = db.query(Category).filter(Category.is_active==True, Category.is_system==False).order_by(Category.name).all()

    if request.method == 'POST':
        action = request.form.get('action')
        comment = request.form.get('comment', '').strip() or None

        # Apply any edits
        before = {
            'date': str(entry.date), 'hours': str(entry.hours),
            'description': entry.description, 'category_id': entry.category_id,
        }
        entry.category_id = int(request.form['category_id'])
        entry.date = date.fromisoformat(request.form['date'])
        entry.hours = float(request.form['hours'])
        entry.description = request.form.get('description', '').strip() or None
        after = {
            'date': str(entry.date), 'hours': str(entry.hours),
            'description': entry.description, 'category_id': entry.category_id,
        }
        changed = before != after

        if action == 'approve':
            entry.status = RecordStatus.approved
            entry.approved_by = session['user_id']
            entry.approved_at = datetime.now()
            db.add(EntryHistory(
                entry_id=entry.id, action='approved',
                performed_by=session['user_id'],
                changes={**(({'edits': {'before': before, 'after': after}} if changed else {})),
                         **({'comment': comment} if comment else {})},
            ))
            db.commit()
            # Push each record in the entry to D4H
            for record in entry.records:
                _push_to_d4h(db, record)
            # Send approval notifications
            for record in entry.records:
                if record.user and record.user.notify_approval == NotifyPref.realtime:
                    from mail import notify_record_approved
                    notify_record_approved(record.user.email, record.user.display_name, entry)
                _check_tax_credit_milestone(db, record.user)
            flash('Entry approved.')

        elif action == 'reject':
            entry.status = RecordStatus.rejected
            db.add(EntryHistory(
                entry_id=entry.id, action='rejected',
                performed_by=session['user_id'],
                changes={**(({'edits': {'before': before, 'after': after}} if changed else {})),
                         **({'comment': comment} if comment else {})},
            ))
            db.commit()
            for record in entry.records:
                if record.user and record.user.notify_approval == NotifyPref.realtime:
                    from mail import notify_record_rejected
                    notify_record_rejected(record.user.email, record.user.display_name,
                                           entry, comment or '')
            flash('Entry rejected.')

        return redirect(url_for('approvals.index'))

    return render_template('approvals/review.html', entry=entry, categories=categories)


@approvals_bp.route('/approvals/<int:entry_id>/approve', methods=['POST'])
@require_role('approver')
def approve(entry_id):
    """Quick approve from the list (no edit)."""
    db = get_db()
    entry = db.query(HoursEntry).filter_by(
        id=entry_id, status=RecordStatus.pending).first()
    if not entry:
        abort(404)
    entry.status = RecordStatus.approved
    entry.approved_by = session['user_id']
    entry.approved_at = datetime.now()
    db.add(EntryHistory(
        entry_id=entry.id, action='approved', performed_by=session['user_id']))
    db.commit()
    for record in entry.records:
        _push_to_d4h(db, record)
        if record.user and record.user.notify_approval == NotifyPref.realtime:
            from mail import notify_record_approved
            notify_record_approved(record.user.email, record.user.display_name, entry)
        _check_tax_credit_milestone(db, record.user)
    flash('Entry approved.')
    return redirect(url_for('approvals.index'))


@approvals_bp.route('/approvals/<int:entry_id>/delete', methods=['POST'])
@require_role('approver')
def delete(entry_id):
    db = get_db()
    entry = db.query(HoursEntry).filter_by(
        id=entry_id, status=RecordStatus.pending).first()
    if not entry:
        abort(404)
    db.delete(entry)  # cascades to records and history
    db.commit()
    flash('Entry deleted.')
    return redirect(url_for('approvals.index'))


@approvals_bp.route('/approvals/<int:entry_id>/reject', methods=['POST'])
@require_role('approver')
def reject(entry_id):
    """Quick reject from the list (no edit)."""
    db = get_db()
    entry = db.query(HoursEntry).filter_by(
        id=entry_id, status=RecordStatus.pending).first()
    if not entry:
        abort(404)
    reason = request.form.get('reason', '').strip()
    entry.status = RecordStatus.rejected
    db.add(EntryHistory(
        entry_id=entry.id, action='rejected', performed_by=session['user_id'],
        changes={'comment': reason or None},
    ))
    db.commit()
    for record in entry.records:
        if record.user and record.user.notify_approval == NotifyPref.realtime:
            from mail import notify_record_rejected
            notify_record_rejected(record.user.email, record.user.display_name, entry, reason)
    flash('Entry rejected.')
    return redirect(url_for('approvals.index'))
