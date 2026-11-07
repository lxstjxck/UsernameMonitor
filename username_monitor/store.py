"""Small SQLite state store with persistent round-robin selection."""
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Callable


DEFINITIVE = {"free", "occupied", "fragment", "invalid", "unavailable"}


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class OutboxEvent:
    id: int
    username: str
    previous_status: str | None
    attempts: int
    source: str = "manual"


class Store:
    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (
                username TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'unknown',
                checked_at TEXT,
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                old_status TEXT,
                new_status TEXT NOT NULL,
                changed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_state_checked ON state(checked_at);
            CREATE TABLE IF NOT EXISTS notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                change_id INTEGER NOT NULL UNIQUE REFERENCES changes(id),
                username TEXT NOT NULL,
                previous_status TEXT,
                status TEXT NOT NULL CHECK(status IN ('pending','delivered','cancelled')),
                created_at TEXT NOT NULL,
                next_attempt_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                delivered_at TEXT,
                last_error_code TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_outbox_due
                ON notification_outbox(status,next_attempt_at,id);
        """)
        # Additive migration: old events retain the original manual format.
        if "source" not in {row[1] for row in self.db.execute("PRAGMA table_info(notification_outbox)")}:
            self.db.execute("ALTER TABLE notification_outbox ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
        self.db.commit()

    def now(self) -> str:
        return self.clock().astimezone(timezone.utc).isoformat(timespec="seconds")

    def close(self) -> None:
        self.db.close()

    def select(self, names: list[str], limit: int) -> list[str]:
        if not names:
            return []
        # INSERT OR IGNORE preserves checked_at between restarts.
        self.db.executemany("INSERT OR IGNORE INTO state(username) VALUES (?)", ((name,) for name in names))
        self.db.commit()
        allowed = set(names)
        # Only configured names are eligible; this also preserves file order on ties.
        rows = self.db.execute("SELECT username, checked_at FROM state").fetchall()
        order = {name: i for i, name in enumerate(names)}
        rows = [(name, checked) for name, checked in rows if name in allowed]
        rows.sort(key=lambda x: (x[1] or "", order[x[0]]))
        return [name for name, _ in rows[:limit]]

    def record(self, name: str, status: str, detail: str = "", *,
               alert_first_free: bool = False, notification_source: str = "manual",
               auto_candidate: bool = False) -> tuple[str | None, bool]:
        if status not in DEFINITIVE:
            raise ValueError("Нельзя сохранять временные ошибки как окончательный статус")
        if notification_source not in ("manual", "auto"):
            raise ValueError("Неверный источник уведомления")
        with self.db:
            row = self.db.execute("SELECT status FROM state WHERE username = ?", (name,)).fetchone()
            previous = row[0] if row and row[0] != "unknown" else None
            now = self.now()
            self.db.execute("""
                INSERT INTO state(username,status,checked_at,detail) VALUES (?,?,?,?)
                ON CONFLICT(username) DO UPDATE SET status=excluded.status,
                checked_at=excluded.checked_at, detail=excluded.detail
            """, (name, status, now, detail))
            changed = previous != status
            if changed:
                change = self.db.execute(
                    "INSERT INTO changes(username,old_status,new_status,changed_at) VALUES (?,?,?,?)",
                    (name, previous, status, now))
                if status == "free" and (previous is not None or alert_first_free):
                    self.db.execute("""
                        INSERT INTO notification_outbox
                            (change_id,username,previous_status,status,created_at,next_attempt_at,source)
                        VALUES (?,?,?,'pending',?,?,?)
                    """, (change.lastrowid, name, previous, now, now, notification_source))
            if status != "free":
                self.db.execute("""
                    UPDATE notification_outbox SET status='cancelled'
                    WHERE username=? AND status='pending'
                """, (name,))
                if previous == "free" and self.db.execute("""SELECT 1 FROM sqlite_master
                        WHERE type='table' AND name='discovery_candidates'""").fetchone():
                    self.db.execute("""UPDATE discovery_candidates SET status='checked',result=?,
                        excluded_reason='definitive_result',next_check_at=NULL
                        WHERE username=? AND status='confirmed_free'""", (status, name))
            if auto_candidate:
                updated = self.db.execute("""UPDATE discovery_candidates SET status=?,result=?,
                    next_check_at=NULL,excluded_reason=? WHERE username=? AND status='checking'""",
                    ("confirmed_free" if status == "free" else "checked", status,
                     None if status == "free" else "definitive_result", name))
                if updated.rowcount != 1:
                    raise RuntimeError("Состояние автоматического кандидата изменилось во время проверки")
        return previous, changed

    def cancel_unwatched(self, names: list[str], auto_names: list[str] | None = None) -> None:
        # Only pending events are cancelled; history and delivered events remain.
        with self.db:
            self.db.execute("CREATE TEMP TABLE IF NOT EXISTS watched_names(username TEXT PRIMARY KEY)")
            self.db.execute("DELETE FROM watched_names")
            self.db.executemany("INSERT OR IGNORE INTO watched_names VALUES (?)", ((name,) for name in names))
            if auto_names is None:
                self.db.execute("""UPDATE notification_outbox SET status='cancelled'
                    WHERE status='pending' AND username NOT IN (SELECT username FROM watched_names)""")
            else:
                self.db.execute("CREATE TEMP TABLE IF NOT EXISTS watched_auto(username TEXT PRIMARY KEY)")
                self.db.execute("DELETE FROM watched_auto")
                self.db.executemany("INSERT OR IGNORE INTO watched_auto VALUES (?)", ((name,) for name in auto_names))
                self.db.execute("""UPDATE notification_outbox SET status='cancelled'
                    WHERE status='pending' AND ((source='manual' AND username NOT IN
                      (SELECT username FROM watched_names)) OR (source='auto' AND username NOT IN
                      (SELECT username FROM watched_auto)))""")

    def due_notifications(self, limit: int) -> list[OutboxEvent]:
        return [OutboxEvent(*row) for row in self.db.execute("""
            SELECT id,username,previous_status,attempts,source FROM notification_outbox
            WHERE status='pending' AND next_attempt_at <= ?
            ORDER BY next_attempt_at,id LIMIT ?
        """, (self.now(), limit)).fetchall()]

    def is_current_free(self, event: OutboxEvent) -> bool:
        row = self.db.execute("SELECT status FROM state WHERE username=?", (event.username,)).fetchone()
        return row is not None and row[0] == "free"

    def cancel_notification(self, event_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE notification_outbox SET status='cancelled' WHERE id=? AND status='pending'",
                            (event_id,))

    def delivered(self, event_id: int) -> None:
        with self.db:
            self.db.execute("""
                UPDATE notification_outbox SET status='delivered',attempts=attempts+1,
                    delivered_at=?,last_error_code=NULL
                WHERE id=? AND status='pending'
            """, (self.now(), event_id))

    def failed(self, event: OutboxEvent, error_code: str = "delivery_failed") -> None:
        # Categories are local constants, never response text or URLs.
        if error_code not in ("delivery_failed",):
            raise ValueError("Неверная категория доставки")
        seconds = min(3600, 30 * 2 ** min(event.attempts, 7))
        next_attempt = (self.clock().astimezone(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")
        with self.db:
            self.db.execute("""
                UPDATE notification_outbox SET attempts=attempts+1,
                    next_attempt_at=?,last_error_code=?
                WHERE id=? AND status='pending'
            """, (next_attempt, error_code, event.id))

    def next_notification_at(self) -> datetime | None:
        row = self.db.execute("""
            SELECT MIN(next_attempt_at) FROM notification_outbox WHERE status='pending'
        """).fetchone()
        return datetime.fromisoformat(row[0]) if row[0] is not None else None

    def mark_error(self, name: str, detail: str) -> None:
        # Preserve last known authoritative status: do not generate false transitions.
        self.db.execute("""
            INSERT INTO state(username,checked_at,detail) VALUES (?,?,?)
            ON CONFLICT(username) DO UPDATE SET checked_at=excluded.checked_at,
            detail=excluded.detail
        """, (name, timestamp(), detail[:100]))
        self.db.commit()

    def summary(self, limit: int = 50) -> list[tuple[str, str, str | None, str]]:
        return self.db.execute("""
            SELECT username, status, checked_at, detail FROM state
            ORDER BY checked_at DESC LIMIT ?
        """, (limit,)).fetchall()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
