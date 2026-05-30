"""
D4H sync for the hours-log home server deployment.

Member-centric approach with parallel API calls:
  1. Fetch all members, detect changes via countRollingHours
  2. Build activity tag cache in parallel (9 tag+type combos)
  3. Fetch attendance per changed member in parallel
  4. Bulk upsert to SQLite

Tag IDs:
  7177 SRVTC Primary Activity   → primary
  7178 SRVTC Secondary Activity → secondary
  7179 Other                    → other
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from decimal import Decimal

logger = logging.getLogger(__name__)

TAG_HOUR_TYPES = [
    (7177, 'primary'),
    (7178, 'secondary'),
    (7179, 'other'),
]
TAG_ID_TO_TYPE = {tid: ht for tid, ht in TAG_HOUR_TYPES}
ACTIVITY_TYPES = ('exercises', 'incidents', 'events')
SYNC_START_DATE = '2026-01-01'
MAX_WORKERS = 8


def _make_client(config: dict):
    from d4h_client import D4HClient
    return D4HClient(
        api_token=config['D4H_API_TOKEN'],
        team_id=config['D4H_TEAM_ID'],
        google_field_id=str(config.get('D4H_GOOGLE_FIELD_ID', '1817')),
    )


def _norm_status(raw: str) -> str:
    return raw.capitalize() if raw else 'Operational'


# ── Activity tag cache ────────────────────────────────────────────────────────

def _fetch_tagged_activities(config: dict, tag_id: int, atype: str) -> dict:
    client = _make_client(config)
    hour_type = TAG_ID_TO_TYPE[tag_id]
    page_size = 250
    result = {}

    try:
        data = client._get(f'/team/{client.team_id}/{atype}',
                           {'size': 1, 'page': 0, 'tag_id': tag_id})
        total = data.get('totalSize', 0)
        logger.debug(f'Tag cache: {atype} tag={tag_id} ({hour_type}) total={total}')
    except Exception as e:
        logger.error(f'D4H sync: count failed {atype} tag={tag_id}: {e}', exc_info=True)
        return result

    total_pages = (total + page_size - 1) // page_size
    for page in range(total_pages - 1, -1, -1):
        try:
            data = client._get(f'/team/{client.team_id}/{atype}',
                               {'size': page_size, 'page': page, 'tag_id': tag_id})
            batch = data.get('results', [])
        except Exception as e:
            logger.error(f'D4H sync: page {page} failed {atype} tag={tag_id}: {e}', exc_info=True)
            continue

        if not batch:
            break
        for a in batch:
            if (a.get('startsAt') or '') >= SYNC_START_DATE:
                result[a['id']] = {
                    'hour_type': hour_type,
                    'name': (a.get('referenceDescription') or a.get('title')
                             or a.get('reference') or str(a['id'])),
                }
        if all((a.get('startsAt') or '') < SYNC_START_DATE for a in batch):
            break

    logger.debug(f'Tag cache: {atype} tag={tag_id} → {len(result)} activities since {SYNC_START_DATE}')
    return result


def _build_activity_tag_cache(config: dict, progress=None) -> dict:
    cache = {}
    combos = [(tag_id, atype) for tag_id, _ in TAG_HOUR_TYPES for atype in ACTIVITY_TYPES]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {
            ex.submit(_fetch_tagged_activities, config, tag_id, atype): (tag_id, atype)
            for tag_id, atype in combos
        }
        done = 0
        for future in as_completed(futures):
            done += 1
            tag_id, atype = futures[future]
            try:
                cache.update(future.result())
            except Exception as e:
                logger.error(f'D4H sync: cache fetch failed tag={tag_id} {atype}: {e}')
            if progress:
                progress(f'Building activity index ({done}/{len(combos)})…',
                         10 + int(15 * done / len(combos)))

    logger.info(f'D4H sync: activity tag cache — {len(cache)} activities')
    return cache


# ── Member attendance ─────────────────────────────────────────────────────────

def _fetch_member_attendance(config: dict, member_id: int, tag_cache: dict) -> list:
    client = _make_client(config)
    records = []
    page = 0
    page_size = 250

    while True:
        try:
            data = client._get(f'/team/{client.team_id}/attendance', {
                'size': page_size, 'page': page,
                'member_id': member_id,
                'sort': 'startsAt', 'order': 'desc',
            })
        except Exception as e:
            logger.error(f'D4H sync: attendance failed member {member_id} page {page}: {e}', exc_info=True)
            break

        batch = data.get('results', [])
        if not batch:
            break

        all_before = True
        _logged_sample = False
        for rec in batch:
            starts = (rec.get('startsAt') or '')[:10]
            if starts >= SYNC_START_DATE:
                all_before = False
                if not _logged_sample:
                    logger.debug(f'Attendance record sample keys: {list(rec.keys())}')
                    logger.debug(f'Attendance record sample: {rec}')
                    _logged_sample = True
                if rec.get('status') != 'ATTENDING':
                    continue
                activity = rec.get('activity') or {}
                activity_id = activity.get('id')
                if not activity_id or activity_id not in tag_cache:
                    continue
                records.append({
                    'attendance_id': rec['id'],
                    'activity_id': activity_id,
                    'activity_type': activity.get('resourceType', '').lower().rstrip('s'),
                    'hour_type': tag_cache[activity_id]['hour_type'],
                    'activity_name': tag_cache[activity_id]['name'],
                    'date': starts,
                    'hours': round((rec.get('duration') or 0) / 60, 2),
                })

        if all_before or len(batch) < page_size:
            break
        page += 1

    logger.debug(f'Attendance member {member_id}: {len(records)} tagged records since {SYNC_START_DATE}')
    return records


# ── Member sync ───────────────────────────────────────────────────────────────

def sync_members(config: dict, db, progress=None) -> dict:
    from models import D4HMember, User

    if progress:
        progress('Fetching members from D4H…', 2)

    client = _make_client(config)
    raw_members = client.get_all_members()

    added = updated = linked = deactivated = reactivated = 0
    changed_ids = set()

    for m in raw_members:
        mid = int(m.id)
        current_rolling = m._raw.get('countRollingHours')
        existing = db.get(D4HMember, mid)
        norm = _norm_status(m.status)

        if existing:
            if existing.count_rolling_hours != current_rolling:
                changed_ids.add(mid)
            was_retired = existing.status == 'Retired'
            is_retired = norm == 'Retired'
            existing.ref = m.ref or 'No Reference'
            existing.name = m.full_name
            existing.email = m.email or None
            existing.google_username = m.google_account.lower() if m.google_account else None
            existing.status = norm
            existing.count_rolling_hours = current_rolling
            existing.last_synced_at = datetime.now()
            if not was_retired and is_retired:
                user = db.query(User).filter_by(d4h_member_id=mid).first()
                if user and user.is_active:
                    user.is_active = False
                    deactivated += 1
            elif was_retired and not is_retired:
                user = db.query(User).filter_by(d4h_member_id=mid).first()
                if user and not user.is_active:
                    user.is_active = True
                    reactivated += 1
            updated += 1
        else:
            changed_ids.add(mid)
            db.add(D4HMember(
                id=mid,
                ref=m.ref or 'No Reference',
                name=m.full_name,
                email=m.email or None,
                google_username=m.google_account.lower() if m.google_account else None,
                status=norm,
                count_rolling_hours=current_rolling,
                last_synced_at=datetime.now(),
            ))
            added += 1

    db.flush()

    for user in db.query(User).filter(User.d4h_member_id == None).all():
        d4h = db.query(D4HMember).filter_by(
            google_username=user.username.lower()).first()
        if d4h:
            user.d4h_member_id = d4h.id
            linked += 1

    db.commit()
    logger.info(
        f'Member sync: {added} added, {updated} updated, {linked} linked, '
        f'{deactivated} deactivated, {reactivated} reactivated, '
        f'{len(changed_ids)} need hours resync'
    )
    return {
        'added': added, 'updated': updated, 'linked': linked,
        'deactivated': deactivated, 'reactivated': reactivated,
        'total': added + updated, 'changed_member_ids': changed_ids,
    }


# ── Hours sync ────────────────────────────────────────────────────────────────

def sync_hours(config: dict, db, changed_member_ids=None, progress=None) -> dict:
    from models import D4HMember, D4HHours, HourType

    tag_cache = _build_activity_tag_cache(config, progress=progress)
    if not tag_cache:
        logger.warning('D4H sync: empty activity cache')
        return {'upserted': 0, 'skipped': 0, 'members_synced': 0}

    all_members = db.query(D4HMember).filter(D4HMember.status != 'Retired').all()
    if changed_member_ids is not None:
        to_sync = [m for m in all_members if m.id in changed_member_ids]
    else:
        to_sync = all_members

    logger.info(f'D4H sync: syncing {len(to_sync)}/{len(all_members)} active members')

    if not to_sync:
        if progress:
            progress('No members with changed hours.', 99)
        return {'upserted': 0, 'skipped': 0, 'members_synced': 0}

    if progress:
        progress(f'Fetching attendance for {len(to_sync)} members…', 26)

    # Parallel attendance fetch
    attendance_by_member = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {
            ex.submit(_fetch_member_attendance, config, m.id, tag_cache): m.id
            for m in to_sync
        }
        done = 0
        for future in as_completed(futures):
            member_id = futures[future]
            done += 1
            try:
                attendance_by_member[member_id] = future.result()
            except Exception as e:
                logger.error(f'D4H sync: member {member_id} failed: {e}')
                attendance_by_member[member_id] = []
            if progress and done % 5 == 0:
                pct = 26 + int(68 * done / len(to_sync))
                progress(f'Fetching attendance ({done}/{len(to_sync)} members)…', pct)

    total_records = sum(len(v) for v in attendance_by_member.values())
    logger.info(f'D4H sync: writing {total_records} attendance records for {len(attendance_by_member)} members')

    # Bulk upsert — commit per member so progress stays live
    total_members = len(attendance_by_member)
    upserted = 0
    for done_count, (member_id, records) in enumerate(attendance_by_member.items(), 1):
        if progress:
            pct = 95 + int(4 * done_count / total_members)
            progress(f'Saving to database ({done_count}/{total_members})…', pct)

        for rec in records:
            existing = db.query(D4HHours).filter_by(
                d4h_attendance_id=rec['attendance_id']).first()
            hour_type = HourType(rec['hour_type'])
            hours = Decimal(str(rec['hours']))
            if existing:
                existing.hour_type = hour_type
                existing.hours = hours
                existing.activity_name = rec['activity_name']
                existing.synced_at = datetime.now()
            else:
                db.add(D4HHours(
                    d4h_attendance_id=rec['attendance_id'],
                    d4h_member_id=member_id,
                    activity_type=rec['activity_type'],
                    d4h_activity_id=rec['activity_id'],
                    activity_name=rec['activity_name'],
                    hour_type=hour_type,
                    date=date.fromisoformat(rec['date']),
                    hours=hours,
                    synced_at=datetime.now(),
                ))
            upserted += 1

        db.commit()

    logger.info(f'Hours sync: {upserted} records across {len(to_sync)} members')
    return {'upserted': upserted, 'skipped': 0, 'members_synced': len(to_sync)}


def sync_all(config: dict, db, progress=None) -> dict:
    def _p(msg, pct):
        if progress:
            progress('members', msg, pct)

    _p('Fetching members from D4H…', 2)
    member_result = sync_members(config, db, progress=lambda msg, pct: _p(msg, pct))
    changed_ids = member_result.get('changed_member_ids')
    _p(f'Members synced. {len(changed_ids)} have new activity.', 10)
    hours_result = sync_hours(
        config, db, changed_member_ids=changed_ids,
        progress=lambda msg, pct: (progress('hours', msg, pct) if progress else None),
    )
    return {'members': member_result, 'hours': hours_result}


def hours_by_year(d4h_hours_list, tool_records_qs, year: int) -> dict:
    from models import RecordStatus, HourType
    totals = {t.value: Decimal('0') for t in HourType}
    for h in d4h_hours_list:
        if h.date.year == year:
            totals[h.hour_type.value] += h.hours
    approved = {RecordStatus.approved, RecordStatus.submitted}
    for r in tool_records_qs:
        if r.date.year == year and r.status in approved:
            ht = r.category.hour_type.value if r.category and r.category.hour_type else 'none'
            totals[ht] += Decimal(str(r.hours))
    totals['tax_credit'] = totals['primary'] + totals['secondary']
    totals['total'] = sum(totals[k] for k in ('primary', 'secondary', 'other', 'none'))
    return totals
