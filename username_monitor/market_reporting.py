"""Read-only experiment reports and explicit cache scoring for untouched candidates."""
from datetime import datetime, timezone

from .market_analysis import analyze_sales, classify, commercial_rating
from .market_data import SOURCE, latest, migrate, utc


def _balanced_top(records, count):
    """Preserve the experimental 40/30/20/10 mix when comparing the same pool."""
    classes = ("word", "semantic", "brand", "thematic")
    fractions = (.4, .3, .2, .1)
    quotas = [round(count * ratio) for ratio in fractions[:3]]
    quotas.append(count - sum(quotas))
    ordered = sorted(records, key=lambda r: (-(r["commercial_score"] if r["commercial_score"] is not None
                                              else r["name_score"]), -r["name_score"], r["username"]))
    chosen = []
    for category, quota in zip(classes, quotas):
        chosen.extend([r for r in ordered if r["category"] == category][:quota])
    picked = {r["username"] for r in chosen}
    chosen.extend(r for r in ordered if r["username"] not in picked and len(chosen) < count)
    return sorted(chosen, key=lambda r: (-(r["commercial_score"] if r["commercial_score"] is not None
                                         else r["name_score"]), r["username"]))


def _evidence(db, snapshot, category, as_of):
    rows = [dict(username=n, price_gram=p, sold_at=t, exclusion_reason=r)
            for n, p, t, r in db.execute("""SELECT username,price_gram,sold_at,exclusion_reason
                FROM market_sales WHERE snapshot_id=? AND sold_at<=?""", (snapshot[0], as_of.isoformat()))]
    return analyze_sales(rows, category, as_of=as_of, complete=snapshot[4] == "complete")


def rating(db, snapshot, name, score, *, as_of):
    evidence = _evidence(db, snapshot, classify(name), as_of) if snapshot else \
        analyze_sales([], classify(name), as_of=as_of, complete=False)
    observed = utc(snapshot[2]) if snapshot else None
    return commercial_rating(name, score, evidence, as_of=as_of, observed_at=observed)


def score_unchecked(db, *, clock=None):
    """Explicit offline operation. Never changes v1 score, check history or Outbox."""
    migrate(db)
    snapshot = latest(db)
    if snapshot is None:
        return 0
    now = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    if utc(snapshot[2]) > now:
        return 0
    rows = db.execute("""SELECT username,score FROM discovery_candidates
        WHERE status='queued' AND attempts=0 AND last_checked_at IS NULL""").fetchall() if db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='discovery_candidates'").fetchone() else []
    count = 0
    with db:
        for name, name_score in rows:
            item = rating(db, snapshot, name, name_score, as_of=now)
            db.execute("""INSERT INTO market_candidate_scores
                (username,commercial_score,status,confidence,reason,source,obtained_at,scored_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET
                commercial_score=excluded.commercial_score,status=excluded.status,
                confidence=excluded.confidence,reason=excluded.reason,source=excluded.source,
                obtained_at=excluded.obtained_at,scored_at=excluded.scored_at""",
                (name, item["score"], item["status"], item["confidence"], item["reason"], SOURCE,
                 snapshot[2], now.isoformat()))
            count += 1
    return count


def compare(db, *, as_of=None, top=20, pool="auto"):
    """Rank the exact same pool twice; old snapshots cannot see later sales."""
    as_of = as_of or datetime.now(timezone.utc)
    if not 1 <= top <= 100:
        raise ValueError("--top должен быть от 1 до 100")
    if pool not in ("auto", "test", "base"):
        raise ValueError("--pool должен быть test или base")
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    test_populated = "market_test_candidates" in names and bool(db.execute(
        "SELECT 1 FROM market_test_candidates LIMIT 1").fetchone())
    table = ("market_test_candidates" if pool == "test" or pool == "auto" and test_populated
             else "discovery_candidates")
    if table not in names:
        return {"status": "no_candidates"}
    if table == "market_test_candidates":
        candidates = db.execute("""SELECT username,name_score FROM market_test_candidates
            WHERE generated_at<=?""", (as_of.isoformat(),)).fetchall()
    else:
        candidates = db.execute("""SELECT username,score FROM discovery_candidates
            WHERE status='queued' AND generated_at<=?""", (as_of.isoformat(),)).fetchall()
    if not candidates:
        return {"status": "no_candidates_at_cutoff", "as_of": as_of.isoformat()}
    # Historical evaluation may use only a snapshot already obtained at the
    # cutoff. In particular, a newly fetched daily file is not past evidence.
    snapshot = latest(db, as_of=as_of)
    records = []
    for name, name_score in candidates:
        item = rating(db, snapshot, name, name_score, as_of=as_of)
        records.append(dict(username=name, category=classify(name), name_score=name_score,
                            commercial_score=item["score"], confidence=item["confidence"],
                            evidence_status=item["status"], reason=item["reason"]))
    v1 = sorted(records, key=lambda r: (-r["name_score"], r["username"]))[:top]
    # With no trusted comparable evidence, the experimental report must also
    # preserve the exact v1 ordering (not just the runtime queue).
    v2 = (_balanced_top(records, min(top, len(records))) if any(
        row["commercial_score"] is not None for row in records) else v1)
    sample = []
    if snapshot:
        sample = [classify(row[0]) for row in db.execute("""SELECT username FROM market_sales
            WHERE snapshot_id=? AND exclusion_reason IS NULL AND length(username)>=5 AND sold_at<=?""",
            (snapshot[0], as_of.isoformat()))]

    def quality(rows):
        return {"meaningful_share": round(sum(r["category"] in ("word", "semantic") for r in rows) / len(rows), 3),
                "distinct_endings": len({r["username"][-3:] for r in rows}),
                "categories": {cat: sum(r["category"] == cat for r in rows)
                               for cat in ("word", "semantic", "brand", "thematic")}}

    # This feed does not provide an unbiased holdout of all sales. Reporting
    # category prediction precision or ROI would be misleading.
    return {"status": "compared", "pool": table, "count": len(records), "as_of": as_of.isoformat(),
            "source": snapshot[1] if snapshot else None,
            "cache_status": "available" if snapshot else "unavailable_at_cutoff",
            "valuation": "insufficient_data" if not snapshot or snapshot[4] != "complete" else "see_each_candidate",
            "observed_sales_sample": {"selection": "top_only_nonrepresentative", "count_5plus": len(sample),
                                      "categories": {cat: sample.count(cat) for cat in
                                                     ("word", "semantic", "brand", "thematic")}},
            "holdout": "not_available_unbiased_sales", "v1": {"top": v1, "quality": quality(v1)},
            "v2": {"top": v2, "quality": quality(v2)}}


def explain(db, name, *, as_of=None):
    as_of = as_of or datetime.now(timezone.utc)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    row = None
    if "market_test_candidates" in tables:
        row = db.execute("""SELECT name_score,reason FROM market_test_candidates
            WHERE username=? AND generated_at<=?""", (name, as_of.isoformat())).fetchone()
    if row is None and "discovery_candidates" in tables:
        row = db.execute("""SELECT score,reason FROM discovery_candidates
            WHERE username=? AND generated_at<=?""", (name, as_of.isoformat())).fetchone()
    if row is None:
        return {"status": "not_found"}
    snapshot = latest(db, as_of=as_of)
    item = rating(db, snapshot, name, row[0], as_of=as_of)
    return {"username": name, "name_score": row[0], "name_reason": row[1],
            "category": classify(name), "commercial": item, "source": snapshot[1] if snapshot else None,
            "obtained_at": snapshot[2] if snapshot else None}
