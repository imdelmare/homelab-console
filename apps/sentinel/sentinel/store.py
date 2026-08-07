from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class Observation:
    dedupe_key: str
    source_id: str
    kind: str
    ok: bool
    title: str
    description: str
    failure_confirmations: int = 1
    notification_group: str = "homelab-console-availability"


@dataclass(frozen=True)
class IncidentChange:
    action: str
    observation: Observation
    occurrences: int


class SentinelStore:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists incidents (
                    dedupe_key text primary key,
                    source_id text not null,
                    kind text not null,
                    title text not null,
                    status text not null,
                    occurrences integer not null,
                    first_seen_at text not null,
                    last_seen_at text not null,
                    resolved_at text,
                    last_error text not null,
                    recovery_notified integer not null default 0
                );
                create index if not exists ix_sentinel_incidents_status
                    on incidents(status);
                create table if not exists heartbeats (
                    source_id text primary key,
                    last_seen_at text not null,
                    remote_addr text not null,
                    payload_json text not null
                );
                """
            )
            columns = {row[1] for row in conn.execute("pragma table_info(incidents)")}
            additions = {
                "failure_streak": "integer not null default 0",
                "success_streak": "integer not null default 0",
                "alert_notified": "integer not null default 0",
                "notification_group": "text not null default 'homelab-console-availability'",
                "last_notified_at": "text",
            }
            for name, definition in additions.items():
                if name not in columns:
                    conn.execute(f"alter table incidents add column {name} {definition}")

    def record_failure(self, observation: Observation) -> IncidentChange:
        now = iso()
        with self._connect() as conn:
            row = conn.execute(
                "select status, occurrences, failure_streak from incidents where dedupe_key = ?",
                (observation.dedupe_key,),
            ).fetchone()
            if row and row["status"] == "open":
                occurrences = int(row["occurrences"]) + 1
                conn.execute(
                    """
                    update incidents
                    set occurrences = ?, last_seen_at = ?, last_error = ?
                    where dedupe_key = ?
                    """,
                    (occurrences, now, observation.description, observation.dedupe_key),
                )
                return IncidentChange("repeated_open", observation, occurrences)

            streak = (
                int(row["failure_streak"] or 0) + 1
                if row and row["status"] == "pending"
                else 1
            )
            status = "open" if streak >= max(1, observation.failure_confirmations) else "pending"
            action = "opened" if status == "open" else "pending_failure"

            # A resolved incident reopening starts a fresh occurrence count rather
            # than resuming the historical one from before it recovered.
            occurrences = 1
            conn.execute(
                """
                insert into incidents (
                    dedupe_key, source_id, kind, title, status, occurrences,
                    first_seen_at, last_seen_at, resolved_at, last_error, recovery_notified,
                    failure_streak, success_streak, alert_notified, notification_group
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, null, ?, 0, ?, 0, 0, ?)
                on conflict(dedupe_key) do update set
                    status = excluded.status,
                    occurrences = excluded.occurrences,
                    first_seen_at = case
                        when incidents.status = 'resolved' then excluded.first_seen_at
                        else incidents.first_seen_at
                    end,
                    last_seen_at = excluded.last_seen_at,
                    resolved_at = null,
                    last_error = excluded.last_error,
                    recovery_notified = 0,
                    failure_streak = excluded.failure_streak,
                    success_streak = 0,
                    alert_notified = case
                        when incidents.status = 'resolved' then 0
                        else incidents.alert_notified
                    end,
                    notification_group = excluded.notification_group
                """,
                (
                    observation.dedupe_key,
                    observation.source_id,
                    observation.kind,
                    observation.title,
                    status,
                    occurrences,
                    now,
                    now,
                    observation.description,
                    streak,
                    observation.notification_group,
                ),
            )
            return IncidentChange(action, observation, occurrences)

    def record_recovery(self, observation: Observation, confirmations: int = 1) -> IncidentChange | None:
        now = iso()
        with self._connect() as conn:
            row = conn.execute(
                """
                select occurrences, status, success_streak from incidents
                where dedupe_key = ? and status in ('open', 'pending')
                """,
                (observation.dedupe_key,),
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "pending":
                conn.execute("delete from incidents where dedupe_key = ?", (observation.dedupe_key,))
                return IncidentChange("cleared_before_open", observation, int(row["occurrences"]))
            success_streak = int(row["success_streak"] or 0) + 1
            if success_streak < max(1, confirmations):
                conn.execute(
                    "update incidents set success_streak = ?, last_seen_at = ? where dedupe_key = ?",
                    (success_streak, now, observation.dedupe_key),
                )
                return IncidentChange("pending_recovery", observation, int(row["occurrences"]))
            conn.execute(
                """
                update incidents
                set status = 'resolved', resolved_at = ?, last_seen_at = ?,
                    last_error = '', recovery_notified = 1
                where dedupe_key = ?
                """,
                (now, now, observation.dedupe_key),
            )
            return IncidentChange("resolved", observation, int(row["occurrences"]))

    def notification_groups(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select pending.notification_group,
                    min(pending.first_seen_at) as first_seen_at,
                    (
                        select max(all_i.last_notified_at) from incidents all_i
                        where all_i.notification_group = pending.notification_group
                    ) as last_notified_at
                from incidents pending
                where pending.status = 'open' and pending.alert_notified = 0
                group by pending.notification_group
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def open_group_incidents(self, group: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "select dedupe_key, title, last_error from incidents where status = 'open' and notification_group = ?",
                (group,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_group_alerted(self, group: str, *, suppressed: bool = False) -> None:
        with self._connect() as conn:
            conn.execute(
                "update incidents set alert_notified = ?, last_notified_at = ? where status = 'open' and notification_group = ?",
                (2 if suppressed else 1, iso(), group),
            )

    def due_recovery_groups(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select distinct notification_group from incidents resolved
                where status = 'resolved' and alert_notified = 1 and recovery_notified = 1
                and not exists (
                    select 1 from incidents open_i
                    where open_i.notification_group = resolved.notification_group
                    and open_i.status = 'open'
                )
                """
            ).fetchall()
        return [str(row[0]) for row in rows]

    def mark_group_recovery_sent(self, group: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "update incidents set recovery_notified = 2 where status = 'resolved' and notification_group = ?",
                (group,),
            )

    def record_heartbeat(self, source_id: str, remote_addr: str, payload: dict[str, Any]) -> None:
        payload_json = json.dumps(payload, sort_keys=True)[:4096]
        with self._connect() as conn:
            conn.execute(
                """
                insert into heartbeats(source_id, last_seen_at, remote_addr, payload_json)
                values (?, ?, ?, ?)
                on conflict(source_id) do update set
                    last_seen_at = excluded.last_seen_at,
                    remote_addr = excluded.remote_addr,
                    payload_json = excluded.payload_json
                """,
                (source_id, iso(), remote_addr, payload_json),
            )

    def last_heartbeat_at(self, source_id: str) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute(
                "select last_seen_at from heartbeats where source_id = ?",
                (source_id,),
            ).fetchone()
        return parse_iso(row["last_seen_at"]) if row else None

    def list_open_incidents(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select dedupe_key, source_id, kind, title, occurrences,
                    first_seen_at, last_seen_at, last_error
                from incidents
                where status = 'open'
                order by last_seen_at desc
                """
            ).fetchall()
        return [dict(row) for row in rows]
