"""SQLite-backed store for precise member join/leave timestamps.

The QQ APIs only expose join_time (second-precision epoch) for members still
in the group, and nothing for leavers. Recording the group_increase /
group_decrease events as they happen gives exact local timestamps, and lets
leaver cards still show join/leave times.
"""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS member_times (
    group_id TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    join_time  TEXT NOT NULL DEFAULT '',
    leave_time TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (group_id, user_id)
)
"""


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class MemberStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def get(self, group_id: str, user_id: str) -> tuple[str, str] | None:
        """Return (join_time, leave_time) texts, or None when unknown."""
        with self._lock:
            row = self._conn.execute(
                "SELECT join_time, leave_time FROM member_times WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def record_join(self, group_id: str, user_id: str, when: datetime) -> None:
        """Precise join observed live; a rejoin overwrites and clears leave_time."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO member_times (group_id, user_id, join_time, leave_time)
                VALUES (?, ?, ?, '')
                ON CONFLICT(group_id, user_id)
                DO UPDATE SET join_time = excluded.join_time, leave_time = ''
                """,
                (group_id, user_id, _fmt(when)),
            )
            self._conn.commit()

    def ensure_join(self, group_id: str, user_id: str, when: datetime) -> None:
        """Backfill join time from the API; an existing (event-precise) value wins."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO member_times (group_id, user_id, join_time, leave_time)
                VALUES (?, ?, ?, '')
                ON CONFLICT(group_id, user_id) DO NOTHING
                """,
                (group_id, user_id, _fmt(when)),
            )
            self._conn.commit()

    def record_leave(self, group_id: str, user_id: str, when: datetime) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO member_times (group_id, user_id, join_time, leave_time)
                VALUES (?, ?, '', ?)
                ON CONFLICT(group_id, user_id)
                DO UPDATE SET leave_time = excluded.leave_time
                """,
                (group_id, user_id, _fmt(when)),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
