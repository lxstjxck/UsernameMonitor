"""Opt-in local test pool, kept separate from the production candidate queue."""
import random
import secrets
from datetime import datetime, timezone

from .discovery import Candidate, generate_candidates, rank_batch, score_name
from .market_analysis import WORDS, THEMES, PREFIXES, SUFFIXES, unsafe_name
from .market_data import migrate

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
