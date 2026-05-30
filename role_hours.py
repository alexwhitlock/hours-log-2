"""
Monthly auto-generation of approved hours records for admin role assignments.
Called from the scheduler in app.py on the last day of each month.
"""

import calendar
import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)


def pro_rated_hours(monthly_hours, year: int, month: int,
                    start_date: date, end_date: date | None) -> float:
    """Return hours for a given month, pro-rated if start/end fall mid-month."""
    days_in_month = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end   = date(year, month, days_in_month)
    effective_start = max(start_date, month_start)
    effective_end   = min(end_date if end_date else month_end, month_end)
    days_active = (effective_end - effective_start).days + 1
    if days_active <= 0:
        return 0.0
    if days_active == days_in_month:
        return float(monthly_hours)
    return round(float(monthly_hours) * days_active / days_in_month, 2)


def is_last_day_of_month(d: date = None) -> bool:
    d = d or date.today()
    return d.day == calendar.monthrange(d.year, d.month)[1]


def generate_monthly_role_hours(db) -> int:
    from sqlalchemy import extract
    from models import AdminRole, AdminRoleAssignment, HoursEntry, HoursRecord, RecordStatus

    today = date.today()
    if not is_last_day_of_month(today):
        return 0

    active = [
        a for a in db.query(AdminRoleAssignment)
        .join(AdminRole)
        .filter(
            AdminRole.is_active == True,
            AdminRoleAssignment.start_date <= today,
        ).all()
        if a.end_date is None or a.end_date >= today
    ]

    generated = 0
    for assignment in active:
        existing = db.query(HoursEntry).filter(
            HoursEntry.auto_role_assignment_id == assignment.id,
            extract('year',  HoursEntry.date) == today.year,
            extract('month', HoursEntry.date) == today.month,
        ).first()
        if existing:
            continue

        role = assignment.admin_role
        hours = pro_rated_hours(role.monthly_hours, today.year, today.month,
                                assignment.start_date, assignment.end_date)
        record_date = today

        entry = HoursEntry(
            submitted_by=assignment.user_id,
            category_id=role.category_id,
            date=record_date,
            hours=hours,
            description=f'Auto: {role.name}',
            status=RecordStatus.approved,
            approved_at=datetime.now(),
            auto_role_assignment_id=assignment.id,
        )
        db.add(entry)
        db.flush()

        record = HoursRecord(entry_id=entry.id, user_id=assignment.user_id)
        db.add(record)

        generated += 1
        logger.info(
            f'Role hours: generated {role.monthly_hours}h for user {assignment.user_id} '
            f'({role.name}) for {today.year}-{today.month:02d}'
        )

    if generated:
        db.commit()
        logger.info(f'Role hours: generated {generated} records for {today}')

    return generated
