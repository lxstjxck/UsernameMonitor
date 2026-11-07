"""Local, bounded candidate generation and durable AutoDiscovery queue."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import random
import re
import secrets
import sqlite3

from .names import USERNAME_PATTERN

VOWELS = "aeiou"
CONSONANTS = "bcdfghjklmnprstvwxyz"
START = ("vel", "nex", "zen", "lum", "nov", "ver", "sol", "riv", "kor", "tek",
         "mir", "tal", "ren", "bel", "dor", "fin", "hal", "gen", "vis", "rav",
         "lin", "mon", "cal", "ser", "vin", "tor", "len", "nav")
MIDDLE = ("a", "e", "i", "o", "u", "ra", "la", "vi", "no", "mi", "ro", "za", "ne", "lu")
END = ("ra", "na", "vo", "lo", "ia", "io", "or", "is", "um", "ex", "en", "ix",
       "ora", "iva", "ena", "aro", "ova", "era", "iro", "ula")
UNWANTED = ("xxx", "qq", "jj", "fuck", "porn", "shit", "nazi")
STATES = ("queued", "checking", "checked", "confirmed_free", "discarded", "retry")
CATEGORIES = ("startup", "short")


@dataclass(frozen=True)
class Candidate:
    username: str
    category: str
    score: int
    reason: str


def score_name(name: str, category: str) -> tuple[int, str] | None:
    if category not in CATEGORIES or not USERNAME_PATTERN.fullmatch(name):
        return None
    if any(bad in name for bad in UNWANTED) or any(name[i] == name[i + 1] == name[i + 2]
                                                   for i in range(len(name) - 2)):
        return None
    runs = [len(chunk) for chunk in re.findall(r"[^aeiou]+", name)]
    if max(runs, default=0) > 2 or len(set(name)) < 4:
        return None
    vowels = sum(char in VOWELS for char in name)
    ratio = vowels / len(name)
    if not .25 <= ratio <= .65:
        return None
    target = 8 if category == "startup" else 5.5
    length = max(0, 28 - 5 * abs(len(name) - target))
    flow = 25 if max(runs, default=0) <= 2 else 12
    memorability = min(20, len(set(name)) * 3)
    vowel_balance = max(0, 17 - int(abs(ratio - .42) * 40))
    category_fit = 10 if (6 <= len(name) <= 10 if category == "startup" else 5 <= len(name) <= 6) else 0
    score = min(100, max(0, round(length + flow + memorability + vowel_balance + category_fit)))
    return score, (f"length={len(name)}; vowel_balance={vowel_balance}; flow={flow}; "
                   f"memorability={memorability}; category_fit={category_fit}")


def generate_candidates(count: int, seed: int | None = None) -> list[Candidate]:
    """Exactly 70/30 startup/short for multiples of ten; fixed iteration cap."""
    if not 1 <= count <= 1000:
        raise ValueError("Количество кандидатов должно быть от 1 до 1000")
    rng = random.Random(seed if seed is not None else secrets.randbits(64))
    result: list[Candidate] = []
    seen: set[str] = set()
    startup_target = round(count * .7)
    for category, target in (("startup", startup_target), ("short", count - startup_target)):
        produced = 0
        for _ in range(max(100, target * 100)):
            if produced >= target:
                break
            if category == "startup":
                name = rng.choice(START) + rng.choice(MIDDLE) + rng.choice(END)
            else:
                # Five or six letters, with one optional terminal consonant.
                name = "".join((rng.choice(CONSONANTS), rng.choice(VOWELS),
                                rng.choice(CONSONANTS), rng.choice(CONSONANTS if rng.randrange(3) == 0 else VOWELS),
                                rng.choice(VOWELS if rng.randrange(3) == 0 else CONSONANTS)))
                if rng.randrange(2):
                    name += rng.choice(VOWELS if name[-1] not in VOWELS else CONSONANTS)
            rating = score_name(name, category)
            if rating is None or name in seen or rating[0] < 55:
                continue
            seen.add(name)
            result.append(Candidate(name, category, *rating))
            produced += 1
        if produced != target:
            raise RuntimeError("Не удалось создать достаточное число кандидатов в лимите генерации")
    return result


class CandidatePool:
    def __init__(self, store, *, clock=None):
        self.db = store.db
        self.clock = clock or store.clock
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS discovery_candidates (
                username TEXT PRIMARY KEY,
                category TEXT NOT NULL CHECK(category IN ('startup','short')),
                score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
                reason TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('queued','checking','checked','confirmed_free','discarded','retry')),
                last_checked_at TEXT,
                result TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                excluded_reason TEXT,
                next_check_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_discovery_queue
                ON discovery_candidates(status,score DESC,next_check_at);
            CREATE TABLE IF NOT EXISTS discovery_attempts (
                id INTEGER PRIMARY KEY, username TEXT NOT NULL, started_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_discovery_attempts_started ON discovery_attempts(started_at);
        """)
        self._has_state = bool(self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='state'").fetchone())

    def now(self):
        return self.clock().astimezone(timezone.utc).isoformat(timespec="seconds")

    def generate(self, count: int, *, seed: int | None = None, pool_size: int = 300) -> int:
        active = self.db.execute("""SELECT COUNT(*) FROM discovery_candidates
            WHERE status IN ('queued','retry','checking')""").fetchone()[0]
        if not 1 <= pool_size <= 1000 or not 0 <= count <= 1000:
            raise ValueError("Количество кандидатов и размер очереди должны быть от 0 до 1000")
        count = min(count, max(0, pool_size - active))
        if count == 0:
            return 0
        added = 0
        # Fixed work bound even when names were generated in an earlier run.
        for round_no in range(12):
            if added >= count:
                break
            candidates = generate_candidates(count - added, None if seed is None else seed + round_no)
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                available = max(0, pool_size - self.db.execute("""SELECT COUNT(*) FROM discovery_candidates
                    WHERE status IN ('queued','retry','checking')""").fetchone()[0])
                for candidate in candidates:
                    if available == 0:
                        break
                    if self._has_state and self.db.execute(
                            "SELECT 1 FROM state WHERE username=?", (candidate.username,)).fetchone():
                        continue
                    cursor = self.db.execute("""INSERT OR IGNORE INTO discovery_candidates
                        (username,category,score,reason,generated_at,status) VALUES (?,?,?,?,?,'queued')""",
                        (candidate.username, candidate.category, candidate.score, candidate.reason, self.now()))
                    added += cursor.rowcount
                    available -= cursor.rowcount
            if available == 0:
                break
        return added

    def fill(self, pool_size: int):
        active = self.db.execute("""SELECT COUNT(*) FROM discovery_candidates
            WHERE status IN ('queued','retry','checking')""").fetchone()[0]
        return self.generate(pool_size - active, pool_size=pool_size)

    def active_names(self) -> list[str]:
        return [row[0] for row in self.db.execute("""SELECT username FROM discovery_candidates
            WHERE status IN ('queued','checking','retry','confirmed_free')""")]

    def remaining(self, daily_limit: int) -> int:
        cutoff = (self.clock().astimezone(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        used = self.db.execute("SELECT COUNT(*) FROM discovery_attempts WHERE started_at > ?", (cutoff,)).fetchone()[0]
        return max(0, daily_limit - used)

    def recover(self):
        # Only the single monitor process calls this. An interrupted attempt was
        # already counted; a recorded definitive result must not be rechecked.
        with self.db:
            cutoff = (self.clock().astimezone(timezone.utc) - timedelta(hours=48)).isoformat(timespec="seconds")
            self.db.execute("DELETE FROM discovery_attempts WHERE started_at < ?", (cutoff,))
            self.db.execute("""UPDATE discovery_candidates SET
                status=CASE WHEN (SELECT status FROM state WHERE state.username=discovery_candidates.username)='free'
                    THEN 'confirmed_free'
                    WHEN (SELECT status FROM state WHERE state.username=discovery_candidates.username)
                         IN ('occupied','unavailable','invalid','fragment') THEN 'checked'
                    WHEN attempts >= 3 THEN 'discarded' ELSE 'retry' END,
                excluded_reason=CASE WHEN attempts >= 3 THEN 'retry_limit' ELSE excluded_reason END,
                next_check_at=CASE WHEN attempts >= 3 THEN NULL ELSE ? END
                WHERE status='checking'""", (self.now(),))
            if self._has_state:
                self.db.execute("""UPDATE discovery_candidates SET status='discarded',
                    excluded_reason='already_checked',next_check_at=NULL
                    WHERE status IN ('queued','retry') AND EXISTS (
                        SELECT 1 FROM state WHERE state.username=discovery_candidates.username
                          AND state.status IN ('free','occupied','unavailable','invalid','fragment'))""")

    def pick(self, manual: list[str], daily_limit: int) -> str | None:
        if self.remaining(daily_limit) == 0:
            return None
        blocked = set(manual)
        return next((name for (name,) in self.db.execute("""SELECT username FROM discovery_candidates
            WHERE status='queued' OR (status='retry' AND next_check_at<=?)
            ORDER BY score DESC, generated_at, username LIMIT 1000""", (self.now(),)) if name not in blocked), None)

    def begin(self, name: str, daily_limit: int) -> bool:
        # Reserve a rolling-window slot at the point the logical RPC starts.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.remaining(daily_limit) == 0:
                return False
            changed = self.db.execute("""UPDATE discovery_candidates SET status='checking',
                attempts=attempts+1,last_checked_at=? WHERE username=? AND
                (status='queued' OR (status='retry' AND next_check_at<=?))""",
                (self.now(), name, self.now()))
            if not changed.rowcount:
                return False
            self.db.execute("INSERT INTO discovery_attempts(username,started_at) VALUES (?,?)", (name, self.now()))
        return True

    def temporary_error(self, name: str, category: str, delay: float):
        with self.db:
            self.db.execute("""UPDATE discovery_candidates SET
                status=CASE WHEN attempts>=3 THEN 'discarded' ELSE 'retry' END,
                result=?,excluded_reason=CASE WHEN attempts>=3 THEN 'retry_limit' ELSE NULL END,
                next_check_at=CASE WHEN attempts>=3 THEN NULL ELSE ? END
                WHERE username=? AND status='checking'""",
                (category, (self.clock().astimezone(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds"), name))


def pool_for(store):
    pool = getattr(store, "_discovery_pool", None)
    if pool is None:
        pool = CandidatePool(store)
        store._discovery_pool = pool
    return pool


def read_pool(path: Path, *, action: str, top: int = 20, category: str | None = None,
              status: str | None = None, daily_limit: int = 12, clock=None) -> str:
    """Read-only CLI report, including for old databases without these tables."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    empty = (f"queued: 0; checked: 0; confirmed_free: 0; discarded: 0; "
             f"daily_remaining: {daily_limit}") if action == "status" else "Candidate Pool: no data"
    if not path.exists():
        return empty
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "discovery_candidates" not in tables:
            return empty
        if action == "status":
            counts = dict(db.execute("SELECT status,COUNT(*) FROM discovery_candidates GROUP BY status"))
            cutoff = (clock().astimezone(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
            used = db.execute("SELECT COUNT(*) FROM discovery_attempts WHERE started_at>?", (cutoff,)).fetchone()[0] \
                if "discovery_attempts" in tables else 0
            return (f"queued: {counts.get('queued',0) + counts.get('retry',0) + counts.get('checking',0)}; "
                    f"checked: {counts.get('checked',0)}; confirmed_free: {counts.get('confirmed_free',0)}; "
                    f"discarded: {counts.get('discarded',0)}; "
                    f"daily_remaining: {max(0,daily_limit-used)}")
        if not 1 <= top <= 1000:
            raise ValueError("--top должен быть от 1 до 1000")
        rows = db.execute("""SELECT username,category,score,status,reason FROM discovery_candidates
            WHERE (? IS NULL OR category=?) AND (? IS NULL OR status=?)
            ORDER BY score DESC,username LIMIT ?""", (category, category, status, status, top)).fetchall()
        return "\n".join(f"@{name} {cat} score={score} {state} {reason}" for name, cat, score, state, reason in rows) \
            or "Candidate Pool: no data"
    finally:
        db.close()
