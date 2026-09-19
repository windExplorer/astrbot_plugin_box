"""SQLite-backed store for precise member join/leave timestamps and card cache.

The QQ APIs only expose join_time (second-precision epoch) for members still
in the group, and nothing for leavers. Recording the group_increase /
group_decrease events as they happen gives exact local timestamps, and lets
leaver cards still show join/leave times.

The card cache table keeps the last fetched card per (group, user) so that
repeated queries within the cooldown window are served without re-hitting
the QQ APIs or the LLM.
"""

import json
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
);
CREATE TABLE IF NOT EXISTS card_cache (
    group_id   TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    display    TEXT NOT NULL DEFAULT '[]',
    level_text TEXT NOT NULL DEFAULT '',
    join_rank  TEXT NOT NULL DEFAULT '',
    analyses   TEXT NOT NULL DEFAULT '{}',
    card_type  TEXT NOT NULL DEFAULT '',
    image      BLOB NOT NULL,
    PRIMARY KEY (group_id, user_id)
);
CREATE TABLE IF NOT EXISTS member_info (
    group_id   TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    card  TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    role  TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
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
            self._conn.executescript(_SCHEMA)
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

    def put_member_info(self, group_id: str, user_id: str, card: str, title: str, role: str, level: str) -> None:
        """Last-known group metadata (title/level/...), for leaver/kick cards."""
        now = _fmt(datetime.now())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO member_info (group_id, user_id, card, title, role, level, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    card = excluded.card,
                    title = excluded.title,
                    role = excluded.role,
                    level = excluded.level,
                    updated_at = excluded.updated_at
                """,
                (group_id, user_id, card, title, role, level, now),
            )
            self._conn.commit()

    def get_member_info(self, group_id: str, user_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT card, title, role, level FROM member_info WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            ).fetchone()
        if not row:
            return None
        return {"card": row[0], "title": row[1], "role": row[2], "level": row[3]}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------- card cache
    def get_card_cache(self, group_id: str, user_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT fetched_at, display, level_text, join_rank, analyses, card_type, image
                FROM card_cache WHERE group_id = ? AND user_id = ?
                """,
                (group_id, user_id),
            ).fetchone()
        if not row:
            return None
        try:
            return {
                "fetched_at": row[0],
                "display": json.loads(row[1]),
                "level_text": row[2],
                "join_rank": row[3],
                "analyses": json.loads(row[4]),
                "card_type": row[5],
                "image": row[6],
            }
        except (ValueError, TypeError):
            return None

    def put_card_cache(
        self,
        group_id: str,
        user_id: str,
        fetched_at: str,
        display: list[str],
        level_text: str,
        join_rank: str,
        analyses: dict[str, str],
        card_type: str,
        image: bytes,
        stale_before: str = "",
    ) -> None:
        """Store/refresh the cached card; rows older than stale_before are dropped."""
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO card_cache
                    (group_id, user_id, fetched_at, display, level_text, join_rank, analyses, card_type, image)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    user_id,
                    fetched_at,
                    json.dumps(display, ensure_ascii=False),
                    level_text,
                    join_rank,
                    json.dumps(analyses, ensure_ascii=False),
                    card_type,
                    image,
                ),
            )
            if stale_before:
                self._conn.execute("DELETE FROM card_cache WHERE fetched_at < ?", (stale_before,))
            self._conn.commit()
