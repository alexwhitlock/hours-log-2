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
    Returns True on success, False on failure (sets d4h_needs_resync on all).
    records is a list of HoursRecord objects."""
    from models import RecordStatus

    member_d4h_id = next(
        (r.user.d4h_member_id for r in records if r.user and r.user.d4h_member_id),
        None,
    )
    if not member_d4h_id:
        logger.warning(f'D4H submit: no D4H member ID for user {records[0].user_id}')
        return False

    # Total hours from associated entries
    total_hours = sum(float(r.entry.hours) for r in records if r.entry)

    try:
        existing_att_id = next(
            (int(r.d4h_record_id) for r in records if r.d4h_record_id), None
        )
        if existing_att_id:
            from models import D4HSubmissionEvent, HourType
            sub_event = db.query(D4HSubmissionEvent).filter_by(
                year=year, month=month, hour_type=HourType(hour_type),
            ).first()
            if sub_event:
                client.set_event_published(sub_event.d4h_event_id, False)
            client.patch_submission_attendance(existing_att_id, total_hours, year, month)
            if sub_event:
                client.set_event_published(sub_event.d4h_event_id, True)
            att_id = str(existing_att_id)
        else:
            sub_event = _get_or_create_event(db, client, year, month, hour_type)
            att = client.create_submission_attendance(
                sub_event.d4h_event_id, member_d4h_id, total_hours, year, month,
            )
            client.set_event_published(sub_event.d4h_event_id, True)
            att_id = str(att['id'])

        from models import EntryHistory
        for r in records:
            r.d4h_record_id = att_id
            r.d4h_needs_resync = False
            if r.entry:
                r.entry.status = RecordStatus.submitted
                db.add(EntryHistory(
                    entry_id=r.entry.id,
                    action='pushed_to_d4h',
                    performed_by=r.user_id,
                    changes={
                        'd4h_event_id': sub_event.d4h_event_id,
                        'd4h_attendance_id': att_id,
                    },
                ))
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
    from models import HoursRecord, HoursEntry, RecordStatus

    # Query HoursRecord joined to HoursEntry where entry.status in (approved, submitted)
    records = (
        db.query(HoursRecord)
        .join(HoursEntry, HoursRecord.entry_id == HoursEntry.id)
        .filter(HoursEntry.status.in_([RecordStatus.approved, RecordStatus.submitted]))
        .all()
    )

    # Filter to submittable types with linked D4H members
    records = [
        r for r in records
        if r.entry
        and r.entry.category
        and r.entry.category.hour_type.value in SUBMITTABLE_TYPES
        and r.user
        and r.user.d4h_member_id
    ]

    # Group by (user_id, hour_type, year, month) using entry metadata
    groups = defaultdict(list)
    for r in records:
        key = (r.user_id, r.entry.category.hour_type.value,
               r.entry.date.year, r.entry.date.month)
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
    from models import HoursRecord, HoursEntry, RecordStatus

    group = [
        r for r in (
            db.query(HoursRecord)
            .join(HoursEntry, HoursRecord.entry_id == HoursEntry.id)
            .filter(
                HoursRecord.user_id == user_id,
                HoursEntry.status.in_([RecordStatus.approved, RecordStatus.submitted]),
            ).all()
        )
        if r.entry
        and r.entry.category
        and r.entry.category.hour_type.value == hour_type
        and r.entry.date.year == year
        and r.entry.date.month == month
    ]
    if not group:
        return True
    client = _make_client(config)
    return _push_group(db, client, group, year, month, hour_type)


# ── Handle delete of a submitted record ──────────────────────────────────────

def handle_submitted_record_delete(db, config: dict, record) -> None:
    """Called before deleting a HoursRecord that is part of a submitted entry.
    Patches or removes the D4H attendance."""
    if not record.d4h_record_id:
        return

    entry = record.entry
    if not entry:
        return

    year  = entry.date.year
    month = entry.date.month
    hour_type = entry.category.hour_type.value if entry.category else None

    if not hour_type or hour_type not in SUBMITTABLE_TYPES:
        return

    from models import HoursRecord, HoursEntry, RecordStatus

    # Find remaining records in the group (excluding the one being deleted)
    remaining = [
        r for r in (
            db.query(HoursRecord)
            .join(HoursEntry, HoursRecord.entry_id == HoursEntry.id)
            .filter(
                HoursRecord.user_id == record.user_id,
                HoursEntry.status.in_([RecordStatus.approved, RecordStatus.submitted]),
                HoursRecord.id != record.id,
            ).all()
        )
        if r.entry
        and r.entry.category
        and r.entry.category.hour_type.value == hour_type
        and r.entry.date.year == year
        and r.entry.date.month == month
    ]

    client = _make_client(config)
    try:
        if remaining:
            total = sum(float(r.entry.hours) for r in remaining if r.entry)
            client.patch_submission_attendance(
                int(record.d4h_record_id), total, year, month,
            )
        else:
            client.delete_submission_attendance(int(record.d4h_record_id))
    except Exception as e:
        logger.error(f'D4H submit: failed to update attendance on delete: {e}')
