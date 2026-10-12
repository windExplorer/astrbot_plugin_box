"""SQLite-backed store for precise member join/leave timestamps and card cache.

The QQ APIs only expose join_time (second-precision epoch) for members still
in the group, and nothing for leavers. Recording the group_increase /
group_decrease events as they happen gives exact local timestamps, and lets
leaver cards still show join/leave times.

The card cache table keeps the last fetched card per (group, user) so that
repeated queries within the cooldown window are served without re-hitting
the QQ APIs or the LLM. It stores the **rendered image**, so anything that
changes the card's look would otherwise keep serving stale pictures until the
cooldown expired; rows therefore carry a ``layout`` stamp
(``CARD_LAYOUT_VERSION:外观指纹``, see ``_layout_stamp``) and only match reads
with the exact same stamp — bumping the version invalidates every cached card,
and the fingerprint invalidates them whenever the user changes a look-related
setting (avatar shape, desensitize, display options, AI switches...).

``group_roster`` stores a snapshot of the members actually present in a group
(one row per member, replaced wholesale on every live member-list fetch). It is
what the panel's coverage numbers are measured against, so records of people
who left without their notice ever being observed do not inflate the counts.
"""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

# 卡片渲染布局版本。**改动卡片外观/字段结构时必须 +1**，否则冷却期内的旧图
# 会被继续缓存命中，用户看到的还是老样子。
#   1 = v0.12.24（卡头昵称独占一行、等级独立行）
#   2 = v0.12.25（空昵称显示「未设置昵称」占位，不再用 QQ 号顶替）
CARD_LAYOUT_VERSION = 2

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
    layout     TEXT NOT NULL DEFAULT '',
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
);
CREATE TABLE IF NOT EXISTS group_roster (
    group_id TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    seen_at  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (group_id, user_id)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
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
            self._ensure_columns()
            self._conn.commit()

    def _ensure_columns(self) -> None:
        """给老库补上后加的列。

        ``CREATE TABLE IF NOT EXISTS`` 对已存在的表是空操作，所以升级时新列不会
        自动出现——没有这一步，INSERT 就会因为「no column named ...」直接报错，
        插件连带崩掉。
        """
        wanted = {
            "card_cache": {"layout": "TEXT NOT NULL DEFAULT ''"},
        }
        for table, columns in wanted.items():
            existing = {
                row[1] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not existing:  # 表不存在（理论上不会，_SCHEMA 已建）
                continue
            for name, decl in columns.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

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

    # ------------------------------------------------------ 批量回填与统计
    def put_member_info_many(self, rows: list[tuple]) -> None:
        """批量写入成员群内信息（rows: group_id, user_id, card, title, role, level）。"""
        now = _fmt(datetime.now())
        data = [(g, u, c, t, r, l, now) for (g, u, c, t, r, l) in rows]
        if not data:
            return
        with self._lock:
            self._conn.executemany(
                """
                INSERT OR REPLACE INTO member_info (group_id, user_id, card, title, role, level, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                data,
            )
            self._conn.commit()

    def ensure_join_many(self, rows: list[tuple]) -> None:
        """批量回填入群时间（rows: group_id, user_id, join_time 文本）。

        只回填 join_time 为空的占位行（历史事件留下的）；已有精确值不被覆盖。
        """
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO member_times (group_id, user_id, join_time, leave_time)
                VALUES (?, ?, ?, '')
                ON CONFLICT(group_id, user_id) DO UPDATE SET join_time = excluded.join_time
                WHERE member_times.join_time = ''
                """,
                rows,
            )
            self._conn.commit()

    def info_counts(self) -> dict[str, int]:
        """各群已记录群内信息的成员数。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT group_id, COUNT(*) FROM member_info GROUP BY group_id"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def join_counts(self) -> dict[str, int]:
        """各群已记录入群时间的成员数。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT group_id, COUNT(*) FROM member_times WHERE join_time != '' GROUP BY group_id"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def member_info_for_group(self, group_id: str) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, card, title, role, level FROM member_info WHERE group_id = ?",
                (group_id,),
            ).fetchall()
        return {r[0]: {"card": r[1], "title": r[2], "role": r[3], "level": r[4]} for r in rows}

    def join_times_for_group(self, group_id: str) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, join_time FROM member_times WHERE group_id = ? AND join_time != ''",
                (group_id,),
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def departed_times_for_group(self, group_id: str) -> dict[str, tuple[str, str]]:
        """已离群成员（有退群/被踢记录）的 (入群时间, 退群时间)。

        用于估算离群者卡片的历史排位，以及「他离群那一刻群里有几个人」。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, join_time, leave_time FROM member_times "
                "WHERE group_id = ? AND leave_time != ''",
                (group_id,),
            ).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    # ------------------------------------------------------ 当前名册快照
    def replace_roster(self, group_id: str, user_ids: list[str], seen_at: str) -> None:
        """整表替换某群的「当前名册」快照。

        名册是「该群此刻实际有哪些人」的权威副本，每次拉到实时成员列表时整体替换，
        天然自愈——历史遗留的幽灵记录（退群事件没观测到）不会留在名册里。
        """
        data = [(group_id, str(u), seen_at) for u in user_ids if str(u)]
        with self._lock:
            self._conn.execute("DELETE FROM group_roster WHERE group_id = ?", (group_id,))
            if data:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO group_roster (group_id, user_id, seen_at) VALUES (?, ?, ?)",
                    data,
                )
            self._conn.commit()

    def roster_stats(self) -> dict[str, dict[str, int]]:
        """按群统计名册对齐情况（一次查询取全部群）。

        返回 ``{group_id: {"present", "missing_info", "missing_join", "missing_any"}}``：
        名册人数、以及名册中缺群内信息 / 缺入群时间 / 两者缺其一的人数。
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT r.group_id,
                       COUNT(*) AS present,
                       SUM(CASE WHEN i.user_id IS NULL THEN 1 ELSE 0 END) AS missing_info,
                       SUM(CASE WHEN t.user_id IS NULL THEN 1 ELSE 0 END) AS missing_join,
                       SUM(CASE WHEN i.user_id IS NULL OR t.user_id IS NULL THEN 1 ELSE 0 END) AS missing_any
                FROM group_roster r
                LEFT JOIN member_info i
                       ON i.group_id = r.group_id AND i.user_id = r.user_id
                LEFT JOIN member_times t
                       ON t.group_id = r.group_id AND t.user_id = r.user_id AND t.join_time != ''
                GROUP BY r.group_id
                """
            ).fetchall()
        return {
            r[0]: {
                "present": int(r[1] or 0),
                "missing_info": int(r[2] or 0),
                "missing_join": int(r[3] or 0),
                "missing_any": int(r[4] or 0),
            }
            for r in rows
        }

    def get_meta(self, key: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else ""

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------- card cache
    @staticmethod
    def _layout_stamp(variant: str = "") -> str:
        """缓存行的布局标记 = 程序版本 + 卡片外观指纹。

        ``variant`` 由调用方（``BoxService.card_variant``）按当前配置算出来：头像
        形状、脱敏、展示字段这些**用户可改**的设置一改，卡片像素/文字就变了，
        旧缓存必须立刻作废，否则用户改完设置还要干等冷却期才看到变化。
        """
        return f"{CARD_LAYOUT_VERSION}:{variant.strip()}"

    def get_card_cache(self, group_id: str, user_id: str, variant: str = "") -> dict | None:
        """Return the cached card, or None when absent/stale/布局或外观不匹配。

        标记对不上（升级前的旧图、改过外观设置的旧卡）就当没有缓存并顺手清掉：
        留着也没用，下一次取数会重新渲染并覆盖它。
        """
        stamp = self._layout_stamp(variant)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT fetched_at, display, level_text, join_rank, analyses, card_type, image
                FROM card_cache WHERE group_id = ? AND user_id = ? AND layout = ?
                """,
                (group_id, user_id, stamp),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "DELETE FROM card_cache WHERE group_id = ? AND user_id = ? AND layout != ?",
                    (group_id, user_id, stamp),
                )
                self._conn.commit()
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
        variant: str = "",
    ) -> None:
        """Store/refresh the cached card; rows older than stale_before are dropped."""
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO card_cache
                    (group_id, user_id, fetched_at, display, level_text, join_rank, analyses,
                     card_type, image, layout)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    self._layout_stamp(variant),
                ),
            )
            if stale_before:
                self._conn.execute("DELETE FROM card_cache WHERE fetched_at < ?", (stale_before,))
            self._conn.commit()
