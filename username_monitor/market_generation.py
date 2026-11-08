"""Opt-in local test pool, kept separate from the production candidate queue."""
import random
import secrets
from datetime import datetime, timezone

from .discovery import Candidate, generate_candidates, rank_batch, score_name
from .market_analysis import WORDS, THEMES, PREFIXES, SUFFIXES, unsafe_name
from .market_data import migrate
from .market_analysis import unsafe_name

MIX = (("word", .40), ("semantic", .30), ("brand", .20), ("thematic", .10))


def _quality(name, kind):
    category = "short" if len(name) <= 6 else "startup"
    rating = score_name(name, category)
    if rating is not None:
        return rating
    # Dictionary words with consonant clusters are legitimate (e.g. "crystal").
    return max(0, 90 - max(0, len(name) - 7) * 3), "dictionary_or_semantic; length=%d" % len(name)


def generate_market(count=300, seed=None, *, peers=()):
    if not 1 <= count <= 300:
        raise ValueError("Market test pool count должен быть от 1 до 300")
    rng = random.Random(seed if seed is not None else secrets.randbits(64))
    targets = [round(count * .4), round(count * .3), round(count * .2)]
    targets.append(count - sum(targets))
    options = [
        list(WORDS),
        [a + b for a in WORDS for b in THEMES if a != b],
        [item.username for item in generate_candidates(min(300, max(60, targets[2] * 2)), seed=seed)],
        [a + b for a in PREFIXES for b in THEMES] + [a + b for a in PREFIXES for b in SUFFIXES],
    ]
    result, seen = [], set(peers)
    for (kind, _), target, names in zip(MIX, targets, options):
        rng.shuffle(names)
        choices = []
        for name in names:
            if name in seen or unsafe_name(name) or not 5 <= len(name) <= 32:
                continue
            seen.add(name)
            score, reason = _quality(name, kind)
            choices.append(Candidate(name, kind, score, reason))
        if len(choices) < target:
            raise RuntimeError("Недостаточно имён для тестового пула")
        # Existing rank_batch only uses category as a label; its score/near-name
        # logic is shared so the comparison is on the same quality scale.
        result.extend(rank_batch(choices, peers=tuple(peers) + tuple(x.username for x in result), limit=target))
    return result


def persist_test_pool(db, count=300, seed=None, *, clock=None):
    migrate(db)
    names = generate_market(count, seed)
    now = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc).isoformat()
    with db:
        # This table is exclusively an explicit disposable experiment. The
        # production discovery_candidates queue and its history are untouched.
        db.execute("DELETE FROM market_test_candidates")
        for item in names:
            db.execute("""INSERT INTO market_test_candidates
                (username,class,name_score,reason,generated_at) VALUES (?,?,?,?,?)""",
                (item.username, item.category, item.score, item.reason, now))
    return names


def promotion_plan(db, *, count=20, pool_size=300, manual=()):
    """Read-only plan. No existing row, recorded attempt or history is touched."""
    if not 1 <= count <= 300 or not 1 <= pool_size <= 300:
        raise ValueError("Продвижение допускает 1–300 имён и пул не более 300")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "market_test_candidates" not in tables:
        return {"capacity": 0, "new_count": 0, "candidates": [], "replaced": [], "reason": "no_test_pool"}
    active = (db.execute("""SELECT COUNT(*) FROM discovery_candidates
        WHERE status IN ('queued','checking','retry')""").fetchone()[0]
        if "discovery_candidates" in tables else 0)
    available = max(0, pool_size - active)
    blocked = set(manual)
    protected = set(manual)
    for table in ("discovery_candidates", "state", "changes", "discovery_attempts", "notification_outbox"):
        if table in tables:
            names = {row[0] for row in db.execute(f"SELECT DISTINCT username FROM {table}")}
            blocked.update(names)
            if table != "discovery_candidates":
                protected.update(names)
    rows = db.execute("""SELECT username,class,name_score,reason FROM market_test_candidates
        ORDER BY name_score DESC,username""").fetchall()
    eligible = [dict(username=name, market_class=kind, name_score=score,
                     reason=f"market_test:{kind}; {reason}") for name, kind, score, reason in rows
                if name not in blocked and not unsafe_name(name)]
    replaceable = []
    if "discovery_candidates" in tables:
        columns = {row[1] for row in db.execute("PRAGMA table_info(discovery_candidates)")}
        origin_filter = "AND origin='v1'" if "origin" in columns else ""
        replaceable = [name for (name,) in db.execute(f"""SELECT username FROM discovery_candidates
            WHERE status='queued' AND attempts=0 AND last_checked_at IS NULL
              {origin_filter} ORDER BY score ASC,generated_at DESC,username""")
            if name not in protected]
    capacity = min(count, len(eligible), available + len(replaceable))
    if not capacity:
        return {"capacity": 0, "new_count": 0, "candidates": [], "replaced": [],
                "reason": "no_eligible_candidates" if not eligible else "pool_full"}
    # Fill requested class proportions where possible, then redistribute
    # unused capacity. Never insert an existing checked or manual username.
    targets = [round(capacity * .4), round(capacity * .3), round(capacity * .2)]
    targets.append(capacity - sum(targets))
    chosen = []
    for kind, target in zip(("word", "semantic", "brand", "thematic"), targets):
        chosen.extend([row for row in eligible if row["market_class"] == kind][:target])
    seen = {row["username"] for row in chosen}
    for row in eligible:
        if len(chosen) == capacity:
            break
        if row["username"] not in seen:
            chosen.append(row)
            seen.add(row["username"])
    replaced = [{"username": name, "reason": "market_promotion_replacement"}
                for name in replaceable[:max(0, len(chosen) - available)]]
    return {"capacity": capacity, "new_count": len(chosen), "candidates": chosen,
            "replaced": replaced, "reason": "ready" if chosen else "no_eligible_candidates"}


def promote(db, *, count=20, pool_size=300, manual=(), clock=None):
    """Atomic explicit promotion. Never updates a row from the old queue."""
    from .discovery import CandidatePool, SCORE_VERSION
    from types import SimpleNamespace

    pool = CandidatePool(SimpleNamespace(db=db, clock=clock or (lambda: datetime.now(timezone.utc))))
    with db:
        db.execute("BEGIN IMMEDIATE")
        plan = promotion_plan(db, count=count, pool_size=pool_size, manual=manual)
        for row in plan["replaced"]:
            changed = db.execute("""UPDATE discovery_candidates
                SET status='discarded',excluded_reason='market_promotion_replacement',next_check_at=NULL
                WHERE username=? AND status='queued' AND attempts=0 AND last_checked_at IS NULL
                  AND origin='v1'""", (row["username"],))
            if changed.rowcount != 1:
                raise RuntimeError("План продвижения устарел; транзакция отменена")
        for row in plan["candidates"]:
            name = row["username"]
            db.execute("""INSERT INTO discovery_candidates
                (username,category,score,reason,generated_at,status,score_version,
                 origin,market_class,promotion_reason)
                VALUES (?,?,?,?,?,'queued',?,'market_test',?,?)""",
                (name, "short" if len(name) <= 6 else "startup", row["name_score"], row["reason"],
                 pool.now(), SCORE_VERSION, row["market_class"], row["reason"]))
    return plan
