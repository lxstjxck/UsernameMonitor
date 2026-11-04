"""Small SQLite state store with persistent round-robin selection."""
import sqlite3
from pathlib import Path
from datetime import datetime, timezone


DEFINITIVE = {"free", "occupied", "fragment", "invalid", "unavailable"}


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
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
        """)
        self.db.commit()

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

    def record(self, name: str, status: str, detail: str = "") -> tuple[str | None, bool]:
        if status not in DEFINITIVE:
            raise ValueError("Нельзя сохранять временные ошибки как окончательный статус")
        row = self.db.execute("SELECT status FROM state WHERE username = ?", (name,)).fetchone()
        previous = row[0] if row and row[0] != "unknown" else None
        now = timestamp()
        self.db.execute("""
            INSERT INTO state(username,status,checked_at,detail) VALUES (?,?,?,?)
            ON CONFLICT(username) DO UPDATE SET status=excluded.status,
            checked_at=excluded.checked_at, detail=excluded.detail
        """, (name, status, now, detail))
        changed = previous != status
        if changed:
            self.db.execute("INSERT INTO changes(username,old_status,new_status,changed_at) VALUES (?,?,?,?)",
                            (name, previous, status, now))
        self.db.commit()
        return previous, changed

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
