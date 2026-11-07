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
       "ora", "iva", "ena", "aro", "ova", "era", "iro", "ula", "ari", "yne",
       "elo", "uno", "ium", "ael", "evo", "ale", "ani", "oli")
PREFIX = ("va", "ne", "zo", "ki", "ma", "lo", "re", "se", "vi", "ta", "no", "lu", "el", "ora",
          "vio", "ava", "mio", "sora")
CORE = ("lumi", "nora", "viro", "sena", "vera", "tali", "mira", "lora", "fino",
        "riva", "zori", "vexa", "nivo", "kora", "lena", "tora", "melo", "nova")
TAIL = ("a", "o", "i", "en", "ix", "on", "um", "io", "el", "ia", "ar", "is")
UNWANTED = ("xxx", "qq", "jj", "fuck", "porn", "shit", "nazi")
STATES = ("queued", "checking", "checked", "confirmed_free", "discarded", "retry")
CATEGORIES = ("startup", "short")
SCORE_VERSION = 2
# Uncomfortable joins are penalized, while ordinary clusters such as br/tr/st
# are allowed. This is a heuristic for Latin-letter brand names, not a dictionary.
AWKWARD_PAIRS = {"lr": 5, "nr": 5, "nl": 5, "nm": 4, "lv": 4, "rv": 3,
                 "vx": 6, "xv": 7, "xl": 5, "xr": 5, "xj": 8, "hz": 7,
                 "jh": 7, "tj": 6, "jd": 7, "gz": 5, "zr": 4, "yk": 4}


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
    if category == "startup":
        length = 18 if 7 <= len(name) <= 9 else (15 if len(name) in (6, 10) else 8)
    else:
        length = 21 if len(name) == 5 else (17 if len(name) == 6 else 8)
    memorability = min(12, round(len(set(name)) * 1.7))
    vowel_balance = max(0, 13 - round(abs(ratio - .42) * 36))
    transitions = sum((name[i] in VOWELS) != (name[i + 1] in VOWELS)
                      for i in range(len(name) - 1))
    flow = round(12 * transitions / (len(name) - 1))
    bigrams = min(8, len({name[i:i + 2] for i in range(len(name) - 1)}))
    category_fit = 10 if (6 <= len(name) <= 10 if category == "startup" else 5 <= len(name) <= 6) else 0
    joins = sum(AWKWARD_PAIRS.get(name[i:i + 2], 0) for i in range(len(name) - 1))
    joins += sum(3 for i in range(len(name) - 1) if name[i] == name[i + 1] and name[i] not in VOWELS)
    repeated_vowels = sum(10 if name[i:i + 2] in ("oo", "ii") else 5
                          for i in range(len(name) - 1) if name[i] == name[i + 1] and name[i] in VOWELS)
    repeated_syllables = sum(7 for i in range(len(name) - 3)
                             if name[i:i + 2] == name[i + 2:i + 4])
    rare_letters = sum(3 if char in "qjx" else 2 for char in name if char in "qjxwy")
    soft_sounds = min(4, sum(char in "lrmnv" for char in name))
    ending = 2 if name[-1] in "aeo" else (1 if name[-1] in "nlr" else 0)
    penalties = joins + repeated_vowels + repeated_syllables + rare_letters
    score = min(100, max(0, 20 + length + flow + memorability + vowel_balance + bigrams +
                        category_fit + soft_sounds + ending - penalties))
    return score, (f"v{SCORE_VERSION} length={len(name)}; vowel_balance={vowel_balance}; "
                   f"flow={flow}; memorability={memorability}; bigrams={bigrams}; "
                   f"category_fit={category_fit}; soft_sounds={soft_sounds}; ending={ending}; "
                   f"joins=-{joins}; repeated_vowels=-{repeated_vowels}; "
                   f"repeated_syllables=-{repeated_syllables}; rare_letters=-{rare_letters}")


def _similarity(a: str, b: str) -> int:
    penalty = 0
    if a[-4:] == b[-4:]:
        penalty += 8
    elif a[-3:] == b[-3:]:
        penalty += 5
    if a[:4] == b[:4]:
        penalty += 5
    if len(a) == len(b):
        distance = sum(left != right for left, right in zip(a, b))
        if distance <= 2:
            penalty += 12 if distance == 1 else 7
    return penalty


def rank_batch(candidates: list[Candidate], peers=(), *, limit: int | None = None) -> list[Candidate]:
    """Greedy quality ranking with a bounded penalty for near neighbors.

    Scores are committed when selected; later candidates cannot retroactively
    change an earlier candidate's score. Existing processed rows are untouched.
    """
    remaining = list(candidates)
    ranked: list[Candidate] = []
    neighbors = [0] * len(remaining)
    existing = tuple(peers)
    suffixes: dict[str, int] = {}
    for peer in existing:
        suffixes[peer[-2:]] = suffixes.get(peer[-2:], 0) + 1
    for index, candidate in enumerate(remaining):
        neighbors[index] = max((_similarity(candidate.username, peer) for peer in existing), default=0)
    while remaining and (limit is None or len(ranked) < limit):
        best = max(range(len(remaining)), key=lambda i: (
            remaining[i].score - neighbors[i] - min(7, suffixes.get(remaining[i].username[-2:], 0)),
            remaining[i].score, -i))
        selected = remaining.pop(best)
        near = neighbors.pop(best)
        suffix = selected.username[-2:]
        crowd = min(7, suffixes.get(suffix, 0))
        ranked.append(Candidate(selected.username, selected.category,
                                max(0, selected.score - near - crowd),
                                f"{selected.reason}; near=-{near}; ending=-{crowd}"))
        suffixes[suffix] = suffixes.get(suffix, 0) + 1
        for index, candidate in enumerate(remaining):
            neighbors[index] = max(neighbors[index], _similarity(candidate.username, selected.username))
    return ranked


def _startup_name(rng: random.Random) -> str:
    template = rng.randrange(6)
    if template == 0:
        return rng.choice(START) + rng.choice(MIDDLE) + rng.choice(END)
    if template == 1:
        return rng.choice(START) + rng.choice(END)
    if template == 2:
        return rng.choice(PREFIX) + rng.choice(CORE) + rng.choice(TAIL)
    if template == 3:
        return rng.choice(CORE) + rng.choice(END)
    if template == 4:
        return rng.choice(PREFIX) + rng.choice(CORE)
    return rng.choice(CORE) + rng.choice(MIDDLE[5:]) + rng.choice(END[12:])


def generate_candidates(count: int, seed: int | None = None, *, peers=()) -> list[Candidate]:
    """Exactly 70/30 startup/short for multiples of ten; fixed iteration cap."""
    if not 1 <= count <= 1000:
        raise ValueError("Количество кандидатов должно быть от 1 до 1000")
    rng = random.Random(seed if seed is not None else secrets.randbits(64))
    result: list[Candidate] = []
    existing = tuple(peers)
    seen: set[str] = set(existing)
    startup_target = round(count * .7)
    for category, target in (("startup", startup_target), ("short", count - startup_target)):
        options: list[Candidate] = []
        option_target = min(4000, max(target * 4, target + 20))
        for _ in range(max(100, option_target * 40)):
            if len(options) >= option_target:
                break
            if category == "startup":
                name = _startup_name(rng)
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
            options.append(Candidate(name, category, *rating))
        if len(options) < target:
            raise RuntimeError("Не удалось создать достаточное число кандидатов в лимите генерации")
        result.extend(rank_batch(options, peers=existing, limit=target))
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
                next_check_at TEXT,
                score_version INTEGER NOT NULL DEFAULT 2
            );
            CREATE INDEX IF NOT EXISTS idx_discovery_queue
                ON discovery_candidates(status,score DESC,next_check_at);
            CREATE TABLE IF NOT EXISTS discovery_attempts (
                id INTEGER PRIMARY KEY, username TEXT NOT NULL, started_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_discovery_attempts_started ON discovery_attempts(started_at);
        """)
        if "score_version" not in {row[1] for row in self.db.execute("PRAGMA table_info(discovery_candidates)")}:
            self.db.execute("ALTER TABLE discovery_candidates ADD COLUMN score_version INTEGER NOT NULL DEFAULT 1")
            self.db.commit()
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
        peers = [row[0] for row in self.db.execute("""SELECT username FROM discovery_candidates
            WHERE status IN ('queued','retry','checking','confirmed_free')""")]
        # Fixed work bound even when names were generated in an earlier run.
        for round_no in range(12):
            if added >= count:
                break
            candidates = generate_candidates(count - added, None if seed is None else seed + round_no,
                                             peers=peers)
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
                        (username,category,score,reason,generated_at,status,score_version)
                        VALUES (?,?,?,?,?,'queued',?)""",
                        (candidate.username, candidate.category, candidate.score, candidate.reason, self.now(),
                         SCORE_VERSION))
                    added += cursor.rowcount
                    available -= cursor.rowcount
                    if cursor.rowcount:
                        peers.append(candidate.username)
            if available == 0:
                break
        return added

    def rescore_unchecked(self) -> int:
        """Explicit migration of never-started queued rows only; history stays intact."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute("""SELECT username,category FROM discovery_candidates
                WHERE status='queued' AND attempts=0 AND last_checked_at IS NULL AND score_version<?
                ORDER BY generated_at,username""", (SCORE_VERSION,)).fetchall()
            candidates = []
            for name, category in rows:
                rating = score_name(name, category)
                if rating is not None:
                    candidates.append(Candidate(name, category, *rating))
            peers = [row[0] for row in self.db.execute("""SELECT username FROM discovery_candidates
                WHERE status IN ('checking','retry','confirmed_free')
                   OR (status='queued' AND score_version>=?)""", (SCORE_VERSION,))]
            for candidate in rank_batch(candidates, peers=peers):
                self.db.execute("""UPDATE discovery_candidates SET score=?,reason=?,score_version=?
                    WHERE username=? AND status='queued' AND attempts=0 AND last_checked_at IS NULL
                      AND score_version<?""",
                    (candidate.score, candidate.reason, SCORE_VERSION, candidate.username, SCORE_VERSION))
        return len(candidates)

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

    def pick(self, manual: list[str], daily_limit: int, *, market: bool = False) -> str | None:
        if self.remaining(daily_limit) == 0:
            return None
        blocked = set(manual)
        if market and self.db.execute("""SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='market_candidate_scores'""").fetchone():
            cutoff = (self.clock().astimezone(timezone.utc) - timedelta(days=30)).isoformat()
            return next((name for (name,) in self.db.execute("""SELECT c.username
                FROM discovery_candidates c LEFT JOIN market_candidate_scores m
                  ON m.username=c.username AND m.obtained_at>=? AND m.scored_at<=?
                WHERE (c.status='queued' OR (c.status='retry' AND c.next_check_at<=?))
                  AND (m.status IS NULL OR m.status!='excluded')
                ORDER BY COALESCE(m.commercial_score,c.score) DESC,c.score DESC,c.generated_at,c.username
                LIMIT 1000""", (cutoff, self.now(), self.now())) if name not in blocked), None)
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
        if action == "top":
            groups = []
            for cat in CATEGORIES:
                rows = db.execute("""SELECT username,score,status,reason FROM discovery_candidates
                    WHERE category=? ORDER BY score DESC,username LIMIT ?""", (cat, top)).fetchall()
                groups.append(f"TOP {top} {cat}:\n" + ("\n".join(
                    f"@{name} score={score} {state} {reason}" for name, score, state, reason in rows)
                    or "no data"))
            return "\n\n".join(groups)
        rows = db.execute("""SELECT username,category,score,status,reason FROM discovery_candidates
            WHERE (? IS NULL OR category=?) AND (? IS NULL OR status=?)
            ORDER BY score DESC,username LIMIT ?""", (category, category, status, status, top)).fetchall()
        return "\n".join(f"@{name} {cat} score={score} {state} {reason}" for name, cat, score, state, reason in rows) \
            or "Candidate Pool: no data"
    finally:
        db.close()
