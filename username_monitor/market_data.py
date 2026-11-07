"""Bounded, explicit import of a public market snapshot into a local SQLite cache.

The TG Gifts daily feed exposes only its *top* sold names, not a representative
transaction tape. Its aggregates and auctions are stored but never used as
individual completed sales for median-based candidate valuation.
"""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

from .market_analysis import unsafe_name

SOURCE_URL = "https://tggifts.com/data/usernames-daily.json"
SOURCE = "Data: TG Gifts (https://tggifts.com)"
MAX_BYTES = 512_000


def utc(value):
    if not isinstance(value, str):
        raise ValueError("Некорректная дата рыночного набора")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Некорректная дата рыночного набора") from None
    if result.tzinfo is None:
        raise ValueError("Дата должна включать UTC offset")
    return result.astimezone(timezone.utc)


def price(value):
    # bool is an int in Python; reject it as well as non-finite/negative values.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1e9:
        raise ValueError("Некорректная рыночная цена")
    return float(value)


def fetch_daily(*, opener=urlopen) -> bytes:
    """Network is used only by the explicit market refresh CLI command."""
    req = Request(SOURCE_URL, headers={"Accept": "application/json", "User-Agent": "UsernameMonitor/0.1"})
    with opener(req, timeout=10) as response:
        if response.status != 200:
            raise ValueError("Рыночный источник недоступен")
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Рыночный набор слишком большой")
    return data


def validate_daily(raw: bytes, *, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    if len(raw) > MAX_BYTES:
        raise ValueError("Рыночный набор слишком большой")
    try:
        doc = json.loads(raw)
        if doc["source"] != "Fragment public sale history and auction list, recorded by TG Gifts (tggifts.com)":
            raise ValueError("Неверный источник рыночного набора")
        generated = utc(doc["generated"])
        sold = doc["sold_24h"]["top"]
        auctions = doc["auctions_ending_24h"]["top"]
        medians = doc["median_7d"]
        if not isinstance(sold, list) or not isinstance(auctions, list) or not isinstance(medians, dict):
            raise TypeError()
        if generated > now + timedelta(minutes=5) or now - generated > timedelta(days=30):
            raise ValueError("Рыночный снимок устарел или датирован будущим")
        if len(sold) > 1000 or len(auctions) > 1000:
            raise ValueError("Слишком много строк рыночного набора")
        for row in sold:
            if not isinstance(row["name"], str) or not row["name"].isascii():
                raise ValueError("Некорректное имя в продажах")
            price(row["price_gram"])
            if not generated - timedelta(days=2) <= utc(row["sold_at"]) <= generated:
                raise ValueError("Продажа вне временного диапазона")
        for row in auctions:
            if not isinstance(row["name"], str) or not row["name"].isascii():
                raise ValueError("Некорректное имя в аукционах")
            price(row["bid_gram"])
            utc(row["ends_at"])
        for bucket, aggregate in medians.items():
            if bucket not in ("len4", "len5", "len6", "words") or not isinstance(aggregate["sales"], int) \
                    or aggregate["sales"] < 0:
                raise ValueError("Некорректный агрегат")
            price(aggregate["median_gram"])
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("Некорректный формат рыночного набора") from None
    return doc


def connect(path: Path, *, readonly=False):
    if readonly:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        db.execute("PRAGMA query_only=ON")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
    return db


def migrate(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, generated_at TEXT NOT NULL,
            fetched_at TEXT NOT NULL, digest TEXT NOT NULL UNIQUE,
            sample_kind TEXT NOT NULL CHECK(sample_kind='top_sales_only')
        );
        CREATE TABLE IF NOT EXISTS market_sales (
            snapshot_id INTEGER NOT NULL REFERENCES market_snapshots(id),
            username TEXT NOT NULL, price_gram REAL NOT NULL,
            sold_at TEXT NOT NULL, exclusion_reason TEXT,
            PRIMARY KEY(snapshot_id, username, sold_at)
        );
        CREATE TABLE IF NOT EXISTS market_auctions (
            snapshot_id INTEGER NOT NULL REFERENCES market_snapshots(id),
            username TEXT NOT NULL, bid_gram REAL NOT NULL, ends_at TEXT NOT NULL,
            PRIMARY KEY(snapshot_id, username, ends_at)
        );
        CREATE TABLE IF NOT EXISTS market_aggregates (
            snapshot_id INTEGER NOT NULL REFERENCES market_snapshots(id),
            bucket TEXT NOT NULL, sales INTEGER NOT NULL, median_gram REAL NOT NULL,
            PRIMARY KEY(snapshot_id,bucket)
        );
        CREATE TABLE IF NOT EXISTS market_candidate_scores (
            username TEXT PRIMARY KEY, commercial_score INTEGER,
            status TEXT NOT NULL, confidence TEXT NOT NULL, reason TEXT NOT NULL,
            source TEXT NOT NULL, obtained_at TEXT NOT NULL, scored_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS market_test_candidates (
            username TEXT PRIMARY KEY, class TEXT NOT NULL, name_score INTEGER NOT NULL,
            reason TEXT NOT NULL, generated_at TEXT NOT NULL
        );
    """)


def import_daily(db, raw: bytes, *, now=None) -> int:
    now = now or datetime.now(timezone.utc)
    doc = validate_daily(raw, now=now)
    digest = hashlib.sha256(raw).hexdigest()
    migrate(db)
    with db:
        cursor = db.execute("""INSERT OR IGNORE INTO market_snapshots
            (source,generated_at,fetched_at,digest,sample_kind) VALUES (?,?,?,?,'top_sales_only')""",
            (SOURCE, utc(doc["generated"]).isoformat(), now.isoformat(), digest))
        if not cursor.rowcount:
            return 0
        snapshot_id = cursor.lastrowid
        seen = set()
        for row in doc["sold_24h"]["top"]:
            name = row["name"].lower()
            key = (name, row["sold_at"])
            reason = ("invalid_or_identity_risk" if unsafe_name(name) else
                      "duplicate_in_snapshot" if key in seen else
                      "unusual_price_requires_review" if price(row["price_gram"]) > 10_000_000 else None)
            seen.add(key)
            # Duplicate entries have one row in the table; retain the exclusion.
            db.execute("""INSERT INTO market_sales VALUES (?,?,?,?,?)
                ON CONFLICT(snapshot_id,username,sold_at) DO UPDATE SET exclusion_reason='duplicate_in_snapshot'""",
                (snapshot_id, name, price(row["price_gram"]), utc(row["sold_at"]).isoformat(), reason))
        for row in doc["auctions_ending_24h"]["top"]:
            db.execute("INSERT OR IGNORE INTO market_auctions VALUES (?,?,?,?)",
                       (snapshot_id, row["name"].lower(), price(row["bid_gram"]),
                        utc(row["ends_at"]).isoformat()))
        for bucket, item in doc["median_7d"].items():
            db.execute("INSERT INTO market_aggregates VALUES (?,?,?,?)",
                       (snapshot_id, bucket, item["sales"], price(item["median_gram"])))
    return 1


def latest(db, *, as_of=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='market_snapshots'").fetchone():
        return None
    if as_of is not None:
        cutoff = as_of.astimezone(timezone.utc).isoformat()
        return db.execute("""SELECT id,source,generated_at,fetched_at,sample_kind FROM market_snapshots
            WHERE generated_at<=? AND fetched_at<=? ORDER BY generated_at DESC,id DESC LIMIT 1""",
            (cutoff, cutoff)).fetchone()
    return db.execute("""SELECT id,source,generated_at,fetched_at,sample_kind FROM market_snapshots
        ORDER BY generated_at DESC,id DESC LIMIT 1""").fetchone()


def market_status(path: Path, *, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    if not path.exists():
        return {"status": "no_data"}
    with closing(connect(path, readonly=True)) as db:
        snap = latest(db)
        if snap is None:
            return {"status": "no_data"}
        sale_count = db.execute("SELECT COUNT(*) FROM market_sales WHERE snapshot_id=?", (snap[0],)).fetchone()[0]
        auction_count = db.execute("SELECT COUNT(*) FROM market_auctions WHERE snapshot_id=?", (snap[0],)).fetchone()[0]
        excluded = dict(db.execute("""SELECT exclusion_reason,COUNT(*) FROM market_sales
            WHERE snapshot_id=? AND exclusion_reason IS NOT NULL GROUP BY exclusion_reason""", (snap[0],)))
        age = max(0, round((now - utc(snap[2])).total_seconds() / 3600, 1))
        return {"status": "cached" if age <= 24 * 30 else "stale", "source": snap[1],
                "generated_at": snap[2], "fetched_at": snap[3], "age_hours": age,
                "sample_kind": snap[4], "completed_sales_top_sample": sale_count,
                "active_auctions": auction_count, "excluded": excluded,
                "valuation": "insufficient_data"}
