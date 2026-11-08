"""Read-only TOP-10 of confirmed AutoDiscovery usernames. No MTProto or HTTP."""

from datetime import datetime, timezone
import sqlite3


def top_confirmed_free(db: sqlite3.Connection, limit: int = 10) -> list[tuple[str, int, str, str | None]]:
    """Return locally confirmed free names, not a live availability guarantee."""
    if not 1 <= limit <= 10:
        raise ValueError("TOP limit must be between 1 and 10")
    if not db.execute("""SELECT 1 FROM sqlite_master
                         WHERE type='table' AND name='discovery_candidates'""").fetchone():
        return []
    return db.execute("""
        SELECT c.username, c.score,
               COALESCE(NULLIF(c.market_class, ''), c.category),
               c.last_checked_at
        FROM discovery_candidates AS c
        JOIN state AS s ON s.username=c.username
        WHERE c.status='confirmed_free' AND c.result='free' AND s.status='free'
        ORDER BY c.score DESC, c.last_checked_at DESC, c.username
        LIMIT ?
    """, (limit,)).fetchall()


def format_top_confirmed_free(db: sqlite3.Connection) -> str:
    """Build a bounded snapshot for an existing durable auto Outbox event."""
    rows = top_confirmed_free(db)
    lines = [f"🏆 ТОП-10 подтверждённых free ({len(rows)}/10):"]
    if not rows:
        lines.append("Пока нет подтверждённых имён.")
    for number, (username, score, category, checked_at) in enumerate(rows, 1):
        checked = "дата неизвестна"
        if checked_at:
            try:
                checked = datetime.fromisoformat(checked_at).astimezone(timezone.utc).strftime("%d.%m %H:%M UTC")
            except ValueError:
                pass
        lines.append(f"{number}. @{username} — {score}/100, {category}; {checked}")
    lines.append("Статусы сохранены на момент проверки: имя могло быть занято позднее. "
                 "Проверяй доступность вручную перед назначением.")
    return "\n".join(lines)
