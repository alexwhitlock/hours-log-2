"""
D4H hours submission.

Nightly (3am) and on-demand push of approved HoursRecords to D4H monthly
summary events. One event per (hour_type, year, month); each member gets
one attendance record per event whose duration equals their total hours
for that type+month.

Deduplication in d4h_sync.py: attendance records on submission events are
skipped during sync using the D4HSubmissionEvent.d4h_event_id set.
"""

import logging
from collections import defaultdict
from datetime import date, datetime

logger = logging.getLogger(__name__)

SUBMITTABLE_TYPES = ('primary', 'secondary', 'other')


# ── Client factory ────────────────────────────────────────────────────────────

def _make_client(config: dict):
    from d4h_client import D4HClient
    return D4HClient(
        api_token=config['D4H_API_TOKEN'],
        team_id=config['D4H_TEAM_ID'],
        google_field_id=str(config.get('D4H_GOOGLE_FIELD_ID', '1817')),
    )


# ── Core group push ───────────────────────────────────────────────────────────

def _get_or_create_event(db, client, year: int, month: int, hour_type: str):
    from models import D4HSubmissionEvent, HourType
    existing = db.query(D4HSubmissionEvent).filter_by(
        year=year, month=month, hour_type=HourType(hour_type),
    ).first()
    if existing:
        return existing

    event = client.create_submission_event(year, month, hour_type)
    row = D4HSubmissionEvent(
        d4h_event_id=event['id'],
        year=year, month=month,
        hour_type=HourType(hour_type),
    )
    db.add(row)
    db.commit()
    return row


def _push_group(db, client, records: list, year: int, month: int,
                hour_type: str) -> bool:
    """Create or PATCH the attendance record for one member+type+month group.
    Returns True on success, False on failure (sets d4h_needs_resync on all)."""
    from models import RecordStatus

    member_d4h_id = next(
        (r.user.d4h_member_id for r in records if r.user and r.user.d4h_member_id),
        None,
    )
    if not member_d4h_id:
        logger.warning(f'D4H submit: no D4H member ID for user {records[0].user_id}')
        return False

    total_hours = sum(float(r.hours) for r in records)

    try:
        existing_att_id = next(
            (int(r.d4h_record_id) for r in records if r.d4h_record_id), None
        )
        if existing_att_id:
            client.patch_submission_attendance(existing_att_id, total_hours, year, month)
            att_id = str(existing_att_id)
        else:
            sub_event = _get_or_create_event(db, client, year, month, hour_type)
            att = client.create_submission_attendance(
                sub_event.d4h_event_id, member_d4h_id, total_hours, year, month,
            )
            att_id = str(att['id'])

        for r in records:
            r.d4h_record_id = att_id
            r.status = RecordStatus.submitted
            r.d4h_needs_resync = False
        db.commit()
        logger.info(
            f'D4H submit: {year}-{month:02d} {hour_type} '
            f'user={records[0].user_id} total={total_hours}h att={att_id}'
        )
        return True

    except Exception as e:
        logger.error(
            f'D4H submit: failed {year}-{month:02d} {hour_type} '
            f'user={records[0].user_id}: {e}'
        )
        for r in records:
            r.d4h_needs_resync = True
        db.commit()
        return False


# ── Full submission run ───────────────────────────────────────────────────────

def run_submission(db, config: dict) -> dict:
    """Push all approved/submitted records that need syncing to D4H.
    Called nightly at 3am and on-demand from admin page."""
    from models import HoursRecord, RecordStatus

    records = (
        db.query(HoursRecord)
        .filter(HoursRecord.status.in_([RecordStatus.approved, RecordStatus.submitted]))
        .all()
    )

    # Filter to submittable types with linked D4H members
    records = [
        r for r in records
        if r.category
        and r.category.hour_type.value in SUBMITTABLE_TYPES
        and r.user
        and r.user.d4h_member_id
    ]

    # Group by (user_id, hour_type, year, month)
    groups = defaultdict(list)
    for r in records:
        key = (r.user_id, r.category.hour_type.value, r.date.year, r.date.month)
        groups[key].append(r)

    client = _make_client(config)
    pushed = failed = skipped = 0

    for (user_id, hour_type, year, month), group in groups.items():
        needs_push = any(r.d4h_record_id is None or r.d4h_needs_resync for r in group)
        if not needs_push:
            skipped += 1
            continue
        ok = _push_group(db, client, group, year, month, hour_type)
        if ok:
            pushed += len(group)
        else:
            failed += len(group)

    logger.info(f'D4H submission run: {pushed} pushed, {failed} failed, {skipped} groups skipped')
    return {'pushed': pushed, 'failed': failed, 'skipped': skipped}


# ── Immediate push on edit ────────────────────────────────────────────────────

def push_group_immediately(db, config: dict, user_id: int, hour_type: str,
                           year: int, month: int) -> bool:
    """Called immediately when an admin edits a submitted record."""
    from models import HoursRecord, RecordStatus

    group = [
        r for r in db.query(HoursRecord).filter(
            HoursRecord.user_id == user_id,
            HoursRecord.status.in_([RecordStatus.approved, RecordStatus.submitted]),
        ).all()
        if r.category
        and r.category.hour_type.value == hour_type
        and r.date.year == year
        and r.date.month == month
    ]
    if not group:
        return True
    client = _make_client(config)
    return _push_group(db, client, group, year, month, hour_type)


# ── Handle delete of a submitted record ──────────────────────────────────────

def handle_submitted_record_delete(db, config: dict, record) -> None:
    """Called before deleting a submitted record. Patches or removes the D4H attendance."""
    from models import HoursRecord, RecordStatus

    if not record.d4h_record_id:
        return

    year  = record.date.year
    month = record.date.month
    hour_type = record.category.hour_type.value if record.category else None

    if not hour_type or hour_type not in SUBMITTABLE_TYPES:
        return

    # Find remaining records in the group (excluding the one being deleted)
    remaining = [
        r for r in db.query(HoursRecord).filter(
            HoursRecord.user_id == record.user_id,
            HoursRecord.status.in_([RecordStatus.approved, RecordStatus.submitted]),
            HoursRecord.id != record.id,
        ).all()
        if r.category
        and r.category.hour_type.value == hour_type
        and r.date.year == year
        and r.date.month == month
    ]

    client = _make_client(config)
    try:
        if remaining:
            total = sum(float(r.hours) for r in remaining)
            client.patch_submission_attendance(
                int(record.d4h_record_id), total, year, month,
            )
        else:
            client.delete_submission_attendance(int(record.d4h_record_id))
    except Exception as e:
        logger.error(f'D4H submit: failed to update attendance on delete: {e}')
