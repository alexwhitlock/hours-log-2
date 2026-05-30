from datetime import datetime

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   session, url_for)

from auth import require_role
from db import get_db
from models import HoursRecord, RecordHistory, RecordStatus, NotifyPref, User

approvals_bp = Blueprint('approvals', __name__)


@approvals_bp.route('/approvals')
@require_role('approver')
def index():
    db = get_db()
    records = (db.query(HoursRecord)
               .filter_by(status=RecordStatus.pending)
               .order_by(HoursRecord.date.desc())
               .all())
    return render_template('approvals/index.html', records=records)


@approvals_bp.route('/approvals/<int:record_id>/approve', methods=['POST'])
@require_role('approver')
def approve(record_id):
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

    if record.user and record.user.notify_approval == NotifyPref.realtime:
        from mail import notify_record_approved
        notify_record_approved(record.user.email, record.user.display_name, record)

    flash('Record approved.')
    return redirect(url_for('approvals.index'))


@approvals_bp.route('/approvals/<int:record_id>/reject', methods=['POST'])
@require_role('approver')
def reject(record_id):
    db = get_db()
    record = db.query(HoursRecord).filter_by(
        id=record_id, status=RecordStatus.pending).first()
    if not record:
        abort(404)
    reason = request.form.get('reason', '').strip()
    record.status = RecordStatus.rejected
    db.add(RecordHistory(
        record_id=record.id,
        action='rejected',
        performed_by=session['user_id'],
        changes={'reason': reason or None},
    ))
    db.commit()

    if record.user and record.user.notify_approval == NotifyPref.realtime:
        from mail import notify_record_rejected
        notify_record_rejected(record.user.email, record.user.display_name, record, reason)

    flash('Record rejected.')
    return redirect(url_for('approvals.index'))
