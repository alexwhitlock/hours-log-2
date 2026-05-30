"""
Monthly auto-generation of approved hours records for admin role assignments.
Called from the scheduler in app.py on the last day of each month.
"""

import calendar
import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)


def is_last_day_of_month(d: date = None) -> bool:
    d = d or date.today()
    return d.day == calendar.monthrange(d.year, d.month)[1]


def generate_monthly_role_hours(db) -> int:
    from sqlalchemy import extract
    from models import AdminRole, AdminRoleAssignment, HoursRecord, RecordStatus

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
        existing = db.query(HoursRecord).filter(
            HoursRecord.auto_role_assignment_id == assignment.id,
            extract('year',  HoursRecord.date) == today.year,
            extract('month', HoursRecord.date) == today.month,
        ).first()
        if existing:
            continue

        role = assignment.admin_role
        record = HoursRecord(
            user_id=assignment.user_id,
            category_id=role.category_id,
            date=today,
            hours=role.monthly_hours,
            description=f'Auto: {role.name}',
            status=RecordStatus.approved,
            approved_at=datetime.now(),
            auto_role_assignment_id=assignment.id,
        )
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
