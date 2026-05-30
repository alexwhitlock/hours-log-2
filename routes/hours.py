from datetime import date, datetime

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   session, url_for)

from auth import require_login
from db import get_db
from models import Category, D4HHours, HoursRecord, RecordHistory, RecordStatus, User

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
    if record.status != RecordStatus.draft:
        flash('Only draft records can be edited.')
        return redirect(url_for('hours.index'))

    categories = db.query(Category).filter_by(is_active=True).order_by(Category.name).all()

    if request.method == 'POST':
        action = request.form.get('action', 'draft')
        before = {
            'date': str(record.date), 'hours': str(record.hours),
            'description': record.description, 'category_id': record.category_id,
        }
        record.category_id = int(request.form['category_id'])
        record.date = date.fromisoformat(request.form['date'])
        record.hours = float(request.form['hours'])
        record.description = request.form.get('description', '').strip() or None
        record.status = RecordStatus.pending if action == 'submit' else RecordStatus.draft
        record.updated_at = datetime.utcnow()
        db.add(RecordHistory(
            record_id=record.id,
            action='submitted' if action == 'submit' else 'edited',
            performed_by=session['user_id'],
            changes={'before': before, 'after': {
                'date': str(record.date), 'hours': str(record.hours),
                'description': record.description, 'category_id': record.category_id,
            }},
        ))
        db.commit()
        flash('Submitted for approval.' if action == 'submit' else 'Draft saved.')
        return redirect(url_for('hours.index'))

    return render_template('hours/form.html', record=record, categories=categories,
                           today=date.today().isoformat())


@hours_bp.route('/hours/<int:record_id>/history')
@require_login
def history(record_id):
    db = get_db()
    record = db.query(HoursRecord).filter_by(
        id=record_id, user_id=session['user_id']).first()
    if not record:
        abort(404)
    return render_template('hours/history.html', record=record)


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

    return render_template('profile.html', user=user,
                           summary=summary, year=year, years=years,
                           pending_count=pending_count,
                           draft_count=draft_count,
                           has_d4h=user.d4h_member_id is not None)
