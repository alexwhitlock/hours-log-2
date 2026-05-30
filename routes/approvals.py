from datetime import datetime, date

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   session, url_for)

from auth import require_role
from db import get_db
from models import Category, D4HHours, HoursRecord, RecordHistory, RecordStatus, NotifyPref, User

approvals_bp = Blueprint('approvals', __name__)


def _push_to_d4h(db, record) -> None:
    """Immediately push the approved record's group to D4H. Failures are flagged for retry."""
    if not record.user or not record.user.d4h_member_id:
        return
    if not record.category or record.category.hour_type.value not in ('primary', 'secondary', 'other'):
        return
    try:
        from flask import current_app
        from d4h_submit import push_group_immediately
        push_group_immediately(
            db, current_app.config['D4H_CONFIG'],
            record.user_id,
            record.category.hour_type.value,
            record.date.year, record.date.month,
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
    records = (db.query(HoursRecord)
               .filter_by(status=RecordStatus.pending)
               .order_by(HoursRecord.date.desc())
               .all())
    return render_template('approvals/index.html', records=records)


@approvals_bp.route('/approvals/<int:record_id>/review', methods=['GET', 'POST'])
@require_role('approver')
def review(record_id):
    db = get_db()
    record = db.query(HoursRecord).filter_by(
        id=record_id, status=RecordStatus.pending).first()
    if not record:
        abort(404)

    categories = db.query(Category).filter(Category.is_active==True, Category.is_system==False).order_by(Category.name).all()

    if request.method == 'POST':
        action = request.form.get('action')
        comment = request.form.get('comment', '').strip() or None

        # Apply any edits
        before = {
            'date': str(record.date), 'hours': str(record.hours),
            'description': record.description, 'category_id': record.category_id,
        }
        record.category_id = int(request.form['category_id'])
        record.date = date.fromisoformat(request.form['date'])
        record.hours = float(request.form['hours'])
        record.description = request.form.get('description', '').strip() or None
        after = {
            'date': str(record.date), 'hours': str(record.hours),
            'description': record.description, 'category_id': record.category_id,
        }
        changed = before != after

        if action == 'approve':
            record.status = RecordStatus.approved
            record.approved_by = session['user_id']
            record.approved_at = datetime.now()
            db.add(RecordHistory(
                record_id=record.id, action='approved',
                performed_by=session['user_id'],
                changes={**(({'edits': {'before': before, 'after': after}} if changed else {})),
                         **({'comment': comment} if comment else {})},
            ))
            db.commit()
            _push_to_d4h(db, record)
            if record.user and record.user.notify_approval == NotifyPref.realtime:
                from mail import notify_record_approved
                notify_record_approved(record.user.email, record.user.display_name, record)
            _check_tax_credit_milestone(db, record.user)
            flash('Record approved.')

        elif action == 'reject':
            record.status = RecordStatus.rejected
            db.add(RecordHistory(
                record_id=record.id, action='rejected',
                performed_by=session['user_id'],
                changes={**(({'edits': {'before': before, 'after': after}} if changed else {})),
                         **({'comment': comment} if comment else {})},
            ))
            db.commit()
            if record.user and record.user.notify_approval == NotifyPref.realtime:
                from mail import notify_record_rejected
                notify_record_rejected(record.user.email, record.user.display_name,
                                       record, comment or '')
            flash('Record rejected.')

        return redirect(url_for('approvals.index'))

    return render_template('approvals/review.html', record=record, categories=categories)


@approvals_bp.route('/approvals/<int:record_id>/approve', methods=['POST'])
@require_role('approver')
def approve(record_id):
    """Quick approve from the list (no edit)."""
    db = get_db()
    record = db.query(HoursRecord).filter_by(
        id=record_id, status=RecordStatus.pending).first()
    if not record:
        abort(404)
    record.status = RecordStatus.approved
    record.approved_by = session['user_id']
    record.approved_at = datetime.now()
    db.add(RecordHistory(
        record_id=record.id, action='approved', performed_by=session['user_id']))
    db.commit()
    _push_to_d4h(db, record)
    if record.user and record.user.notify_approval == NotifyPref.realtime:
        from mail import notify_record_approved
        notify_record_approved(record.user.email, record.user.display_name, record)
    _check_tax_credit_milestone(db, record.user)
    flash('Record approved.')
    return redirect(url_for('approvals.index'))


@approvals_bp.route('/approvals/<int:record_id>/delete', methods=['POST'])
@require_role('approver')
def delete(record_id):
    db = get_db()
    record = db.query(HoursRecord).filter_by(
        id=record_id, status=RecordStatus.pending).first()
    if not record:
        abort(404)
    from models import RecordHistory
    db.query(RecordHistory).filter_by(record_id=record_id).delete()
    db.delete(record)
    db.commit()
    flash('Record deleted.')
    return redirect(url_for('approvals.index'))


@approvals_bp.route('/approvals/<int:record_id>/reject', methods=['POST'])
@require_role('approver')
def reject(record_id):
    """Quick reject from the list (no edit)."""
    db = get_db()
    record = db.query(HoursRecord).filter_by(
        id=record_id, status=RecordStatus.pending).first()
    if not record:
        abort(404)
    reason = request.form.get('reason', '').strip()
    record.status = RecordStatus.rejected
    db.add(RecordHistory(
        record_id=record.id, action='rejected', performed_by=session['user_id'],
        changes={'comment': reason or None},
    ))
    db.commit()
    if record.user and record.user.notify_approval == NotifyPref.realtime:
        from mail import notify_record_rejected
        notify_record_rejected(record.user.email, record.user.display_name, record, reason)
    flash('Record rejected.')
    return redirect(url_for('approvals.index'))
