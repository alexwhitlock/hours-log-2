"""
D4H API v3 client — canonical shared implementation.

Used by all services and jobs in this project:
  - services/chatbot  (via services/chatbot/d4h_client.py shim)
  - services/account_provisioning
  - jobs/d4h_workspace_sync
  - jobs/d4h_inreach_sync

API base: https://api.team-manager.ca.d4h.com/v3
"""

import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)


class D4HMember:
    def __init__(self, raw: dict, google_field_id: str = None):
        self.id  = str(raw.get("id", ""))
        self.ref = str(raw.get("ref") or "")
        # name is a plain string "First Last" in v3, but may be a dict in other contexts
        name_raw = raw.get("name")
        if isinstance(name_raw, dict):
            self.first_name = (name_raw.get("first") or raw.get("firstName") or "").strip()
            self.last_name  = (name_raw.get("last")  or raw.get("lastName")  or "").strip()
        elif isinstance(name_raw, str):
            parts = name_raw.strip().split(None, 1)
            self.first_name = parts[0] if parts else ""
            self.last_name  = parts[1] if len(parts) > 1 else ""
        else:
            self.first_name = (raw.get("firstName") or "").strip()
            self.last_name  = (raw.get("lastName")  or "").strip()
        # email is a dict {"value": "...", "verified": ...} in v3
        email_raw = raw.get("email")
        self.email = (
            (email_raw.get("value") if isinstance(email_raw, dict) else email_raw) or ""
        ).strip().lower()
        self.status     = (raw.get("status") or "").strip()
        self.position   = raw.get("position", "")
        self._raw       = raw
        # Pull google account from custom fields if field ID known
        self.google_account = ""
        if google_field_id:
            for cf in raw.get("customFieldValues", []):
                if str(cf.get("customField", {}).get("id")) == str(google_field_id):
                    self.google_account = (cf.get("value") or "").strip().lower()
                    break

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def __repr__(self):
        return f"<D4HMember {self.id} {self.full_name!r} status={self.status!r} google={self.google_account!r}>"


class D4HClient:
    def __init__(self, api_token: str, team_id: str,
                 google_field_id: str = "",
                 base_url: str = "https://api.team-manager.ca.d4h.com/v3"):
        self.team_id         = str(team_id).strip()
        self.base_url        = base_url.rstrip("/")
        self.google_field_id = google_field_id
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict = None) -> dict:
        url = f"{self.base_url}{path}"
        logger.debug(f'GET {url} params={params}')
        resp = self.session.get(url, params=params, timeout=30)
        if not resp.ok:
            logger.error(f'D4H API error {resp.status_code} for GET {url}: {resp.text[:200]}')
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, body: dict) -> dict:
        url = f"{self.base_url}{path}"
        resp = self.session.patch(url, json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, body: dict) -> dict:
        url = f"{self.base_url}{path}"
        resp = self.session.put(url, json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self.base_url}{path}"
        resp = self.session.post(url, json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> None:
        url = f"{self.base_url}{path}"
        resp = self.session.delete(url, timeout=30)
        resp.raise_for_status()

    def _paginate(self, path: str, params: dict = None) -> list:
        """Fetch all pages from a paginated D4H v3 endpoint."""
        results = []
        page = 0
        page_size = 250
        base_params = dict(params or {})
        while True:
            base_params.update({"size": page_size, "page": page})
            data = self._get(path, base_params)
            batch = data.get("results", data if isinstance(data, list) else [])
            if not batch:
                break
            results.extend(batch)
            total = data.get("totalSize", len(results))
            if len(results) >= total or len(batch) < page_size:
                break
            page += 1
        return results

    # ------------------------------------------------------------------
    # Members
    # ------------------------------------------------------------------

    def get_all_members(self) -> list[D4HMember]:
        raw = self._paginate(f"/team/{self.team_id}/members")
        members = [D4HMember(m, self.google_field_id) for m in raw]
        logger.info(f"D4H: fetched {len(members)} members")
        return members

    def get_members_by_refs(self, refs: list[str]) -> list[D4HMember]:
        """Return members matching the given D4H Ref numbers. Loads all members once."""
        ref_set = {str(r).strip() for r in refs if str(r).strip()}
        all_members = self.get_all_members()
        matched = [m for m in all_members if m.ref in ref_set]
        not_found = ref_set - {m.ref for m in matched}
        if not_found:
            logger.warning(f"D4H refs not found: {sorted(not_found)}")
        return matched

    def get_member(self, member_id: str) -> D4HMember:
        data = self._get(f"/team/{self.team_id}/members/{member_id}")
        return D4HMember(data.get("data", data), self.google_field_id)

    # ------------------------------------------------------------------
    # Custom field writeback
    # ------------------------------------------------------------------

    def set_google_account(self, member_id: str, google_email: str) -> None:
        """Write the Google Account email back to the D4H custom field."""
        if not self.google_field_id:
            logger.warning("D4H_GOOGLE_FIELD_ID not set — cannot write back Google account")
            return
        body = {
            "customFieldValues": [
                {"id": int(self.google_field_id), "value": google_email}
            ]
        }
        self._patch(f"/team/{self.team_id}/members/{member_id}", body)
        logger.info(f"D4H: wrote Google account {google_email!r} to member {member_id}")

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    def get_groups(self) -> list[dict]:
        """Return all team groups as raw dicts."""
        return self._paginate(f"/team/{self.team_id}/member-groups")

    def find_group(self, query: str) -> Optional[dict]:
        """Find a group by ID or case-insensitive title match."""
        q = query.strip()
        q_lower = q.lower()
        for g in self.get_groups():
            if str(g.get("id")) == q:
                return g
            if (g.get("title") or g.get("name") or "").lower() == q_lower:
                return g
        if q.isdigit():
            return {"id": int(q), "title": q}
        return None

    def get_group_members(self, group_id: str) -> list[D4HMember]:
        """Return all members of a group as D4HMember objects (with full data)."""
        memberships = self._paginate(
            f"/team/{self.team_id}/member-group-memberships",
            {"group_id": group_id},
        )
        member_ids = set()
        for m in memberships:
            if not isinstance(m, dict):
                continue
            member_ref = m.get("member")
            if isinstance(member_ref, dict):
                mid = str(member_ref.get("id") or "")
            elif member_ref is not None:
                mid = str(member_ref)
            else:
                mid = str(m.get("memberId") or "")
            if mid:
                member_ids.add(mid)
        member_ids.discard("")

        if not member_ids:
            logger.info(f"D4H: group {group_id} has 0 members")
            return []

        members = [self.get_member(mid) for mid in sorted(member_ids)]
        logger.info(f"D4H: group {group_id} has {len(members)} members")
        return members

    def get_group_memberships(self, group_id: str) -> dict:
        """
        Return {member_id: membership_id} for a group.
        Used when membership IDs are needed to remove members.
        """
        raw = self._paginate(
            f"/team/{self.team_id}/member-group-memberships",
            {"group_id": group_id},
        )
        result = {}
        for m in raw:
            member_ref = m.get("member")
            mid = str(member_ref.get("id") if isinstance(member_ref, dict) else member_ref or "")
            membership_id = str(m.get("id") or "")
            if mid and membership_id:
                result[mid] = membership_id
        return result

    def add_member_to_group(self, group_id: str, member_id: str) -> dict:
        """Add a member to a group. Returns the created membership."""
        return self._post(
            f"/team/{self.team_id}/member-group-memberships",
            {"groupId": int(group_id), "memberId": int(member_id)},
        )

    def remove_member_group_membership(self, membership_id: str) -> None:
        """Remove a member from a group by membership ID."""
        self._delete(f"/team/{self.team_id}/member-group-memberships/{membership_id}")

    # ------------------------------------------------------------------
    # Activities / attendance
    # ------------------------------------------------------------------

    def get_activity(self, activity_id: str) -> dict:
        """
        Fetch activity info. D4H v3 has no generic /activities endpoint.
        Discovers the resource type from a sample attendance record, then fetches
        the typed endpoint (/incidents/{id}, /exercises/{id}, or /events/{id}).
        Returns the activity dict with a normalised "title" key.
        """
        _TYPE_PATH = {
            "Incident": "incidents",
            "Exercise": "exercises",
            "Event":    "events",
        }
        try:
            sample = self._get(
                f"/team/{self.team_id}/attendance",
                {"activity_id": activity_id, "size": 1, "page": 0},
            )
            records = sample.get("results", sample if isinstance(sample, list) else [])
            if records:
                resource_type = records[0].get("activity", {}).get("resourceType", "")
                path = _TYPE_PATH.get(resource_type)
                if path:
                    data = self._get(f"/team/{self.team_id}/{path}/{activity_id}")
                    result = dict(data.get("data", data.get("results", data)))
                    if not result.get("title") and not result.get("name"):
                        result["title"] = (
                            result.get("referenceDescription")
                            or result.get("reference")
                            or activity_id
                        )
                    return result
        except Exception as e:
            logger.debug(f"D4H: could not fetch activity {activity_id} info: {e}")
        return {}

    # ------------------------------------------------------------------
    # Hours Log submission
    # ------------------------------------------------------------------

    # Maps hour_type value → D4H tag ID
    HOUR_TYPE_TAG = {'primary': 7177, 'secondary': 7178, 'other': 7179}

    def create_submission_event(self, year: int, month: int, hour_type: str) -> dict:
        """Create a monthly Hours Log placeholder event and tag it."""
        import calendar
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo('America/Toronto')
        last_day = calendar.monthrange(year, month)[1]
        label = hour_type.capitalize()
        starts = datetime(year, month, 1, 0, 0, 0, tzinfo=tz)
        ends   = datetime(year, month, last_day, 23, 59, 59, tzinfo=tz)
        event = self._post(f'/team/{self.team_id}/events', {
            'referenceDescription': f'Hours Log: {label} - {year:04d}-{month:02d}',
            'startsAt': starts.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'endsAt':   ends.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'fullTeam': False,
        })
        tag_id = self.HOUR_TYPE_TAG.get(hour_type)
        if tag_id:
            try:
                self._post(f'/team/{self.team_id}/events/{event["id"]}/tags',
                           {'tagIds': [tag_id]})
            except Exception as e:
                logger.warning(f'D4H: could not set tag on event {event["id"]}: {e}')
        logger.info(f'D4H: created submission event {event["id"]} '
                    f'{year:04d}-{month:02d} {hour_type}')
        return event

    def create_submission_attendance(self, event_id: int, member_d4h_id: int,
                                     total_hours: float, year: int, month: int) -> dict:
        from datetime import datetime, timedelta
        start_dt = datetime(year, month, 1)
        end_dt   = start_dt + timedelta(hours=total_hours)
        return self._post(f'/team/{self.team_id}/attendance', {
            'activityId': event_id,
            'memberId':   member_d4h_id,
            'startsAt':   start_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'endsAt':     end_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'status':     'ATTENDING',
        })

    def patch_submission_attendance(self, attendance_id: int,
                                    total_hours: float, year: int, month: int) -> dict:
        from datetime import datetime, timedelta
        start_dt = datetime(year, month, 1)
        end_dt   = start_dt + timedelta(hours=total_hours)
        return self._patch(f'/team/{self.team_id}/attendance/{attendance_id}', {
            'startsAt': start_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'endsAt':   end_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'status':   'ATTENDING',
        })

    def delete_submission_attendance(self, attendance_id: int) -> None:
        self._delete(f'/team/{self.team_id}/attendance/{attendance_id}')

    def get_activity_attendance(self, activity_id: str) -> list[D4HMember]:
        """Return members attending an activity (status == ATTENDING)."""
        raw = self._paginate(
            f"/team/{self.team_id}/attendance",
            {"activity_id": activity_id},
        )
        member_ids = set()
        for record in raw:
            status = (record.get("status") or "").upper()
            if status != "ATTENDING":
                continue
            member_ref = record.get("member")
            if isinstance(member_ref, dict):
                mid = str(member_ref.get("id") or "")
            else:
                mid = str(member_ref or "")
            if mid:
                member_ids.add(mid)
        member_ids.discard("")

        if not member_ids:
            logger.info(f"D4H: activity {activity_id} has 0 attending members")
            return []

        members = [self.get_member(mid) for mid in sorted(member_ids)]
        logger.info(f"D4H: activity {activity_id} has {len(members)} attending members")
        return members
