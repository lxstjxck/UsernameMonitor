import io
import json
import os
import sqlite3
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from username_monitor.__main__ import main
from username_monitor.discovery import CandidatePool
from username_monitor.market_analysis import Evidence, analyze_sales, commercial_rating, unsafe_name
from username_monitor.market_data import import_daily, latest, market_status, market_exclusions, migrate, validate_daily
from username_monitor.market_generation import generate_market, persist_test_pool, promotion_plan, promote
from username_monitor.market_reporting import compare, explain, score_unchecked
from username_monitor.store import Store


NOW = datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc)


def feed():
    return json.dumps({
        "source": "Fragment public sale history and auction list, recorded by TG Gifts (tggifts.com)",
        "generated": "2026-11-07T22:46:56Z",
        "sold_24h": {"count": 623, "top": [
            {"name": "disc", "price_gram": 16000, "sold_at": "2026-11-07T19:57:59Z"},
            {"name": "descent", "price_gram": 1916, "sold_at": "2026-11-07T06:13:02Z"},
            {"name": "telegramofficial", "price_gram": 10, "sold_at": "2026-11-07T05:00:00Z"}]},
        "median_7d": {"len4": {"sales": 34, "median_gram": 6732},
                      "len5": {"sales": 448, "median_gram": 10},
                      "words": {"sales": 11, "median_gram": 1802}},
        "auctions_ending_24h": {"count": 465, "top": [
            {"name": "wildest", "bid_gram": 950, "ends_at": "2026-11-07T22:55:45Z"}]}
    }).encode()


class MarketCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "test.sqlite3"

    def test_feed_validation_separates_sales_bids_and_includes_provenance(self):
        with sqlite3.connect(self.path) as db:
            self.assertEqual(import_daily(db, feed(), now=NOW), 1)
            self.assertEqual(import_daily(db, feed(), now=NOW), 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM market_sales").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM market_auctions").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT exclusion_reason FROM market_sales WHERE username='telegramofficial'").fetchone()[0],
                             "invalid_or_identity_risk")
            self.assertEqual(market_exclusions(db)["excluded"][0]["kind"], "sale")
            self.assertEqual(db.execute("SELECT price_gram FROM market_sales WHERE username='disc'").fetchone()[0], 16000)
            self.assertEqual(db.execute("SELECT bid_gram FROM market_auctions").fetchone()[0], 950)
            self.assertEqual(db.execute("SELECT median_gram FROM market_aggregates WHERE bucket='len4'").fetchone()[0], 6732)
            self.assertEqual(latest(db)[4], "top_sales_only")
        status = market_status(self.path, now=NOW)
        self.assertEqual(status["valuation"], "insufficient_data")
        self.assertEqual(status["active_auctions"], 1)
        self.assertEqual(status["sample_kind"], "top_sales_only")

    def test_bad_feed_never_replaces_last_verified_cache(self):
        with sqlite3.connect(self.path) as db:
            import_daily(db, feed(), now=NOW)
            for change in (lambda d: d["sold_24h"]["top"][0].update(price_gram=-1),
                           lambda d: d["auctions_ending_24h"]["top"][0].update(bid_gram=0),
                           lambda d: d.update(generated="2028-01-01T00:00:00Z")):
                doc = json.loads(feed())
                change(doc)
                with self.assertRaises(ValueError):
                    import_daily(db, json.dumps(doc).encode(), now=NOW)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0], 1)
        self.assertEqual(market_status(self.path, now=NOW + timedelta(days=31))["status"], "stale")

    def test_duplicate_and_suspicious_sale_keep_exclusion_reasons(self):
        doc = json.loads(feed())
        doc["sold_24h"]["top"].append(dict(doc["sold_24h"]["top"][1]))
        doc["sold_24h"]["top"].append({"name": "luminous", "price_gram": 50_000_000,
                                         "sold_at": "2026-11-07T12:00:00Z"})
        with sqlite3.connect(self.path) as db:
            import_daily(db, json.dumps(doc).encode(), now=NOW)
            reasons = dict(db.execute("SELECT username,exclusion_reason FROM market_sales"))
            self.assertEqual(reasons["descent"], "duplicate_in_snapshot")
            self.assertEqual(reasons["luminous"], "unusual_price_requires_review")
            self.assertEqual({item["reason"] for item in market_exclusions(db)["excluded"]},
                             {"duplicate_in_snapshot", "unusual_price_requires_review", "invalid_or_identity_risk",
                              "collectible_length4_not_comparable"})

    def test_existing_v2_cache_gets_additive_exclusion_migration(self):
        with sqlite3.connect(self.path) as db:
            import_daily(db, feed(), now=NOW)
            db.execute("DROP TABLE market_exclusions")
            self.assertIn("invalid_or_identity_risk",
                          {entry["reason"] for entry in market_exclusions(db)["excluded"]})
            original_sales = db.execute("SELECT COUNT(*) FROM market_sales").fetchone()[0]
            original_excluded = db.execute("SELECT COUNT(*) FROM market_sales WHERE exclusion_reason IS NOT NULL"
                                           ).fetchone()[0]
            migrate(db)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM market_sales").fetchone()[0], original_sales)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM market_exclusions").fetchone()[0], original_excluded)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_market_generator_mix_seed_and_safety(self):
        first = generate_market(300, 42)
        self.assertEqual(first, generate_market(300, 42))
        self.assertEqual(Counter(x.category for x in first),
                         {"word": 120, "semantic": 90, "brand": 60, "thematic": 30})
        self.assertEqual(len({x.username for x in first}), 300)
        self.assertTrue(all(5 <= len(x.username) <= 32 and not unsafe_name(x.username)
                            and 0 <= x.score <= 100 for x in first))
        self.assertTrue(unsafe_name("telegramofficial"))
        self.assertTrue(unsafe_name("openai_studio"))
        self.assertTrue(unsafe_name("g00gle_support"))

    def test_completed_comparables_are_temporally_separate_from_bids_and_len4(self):
        rows = [dict(username=n, sold_at=(NOW - timedelta(days=i * 2)).isoformat(),
                     price_gram=p, exclusion_reason=None) for i, (n, p) in enumerate(zip(
            ("amber", "atlas", "bloom", "coral", "craft", "dawn", "cloud"),
            (10, 12, 13, 15, 14, 10000, 11)))]
        rows.append(dict(username="disc", sold_at=NOW.isoformat(), price_gram=1e6, exclusion_reason=None))
        rows.append(dict(username="flora", sold_at=(NOW + timedelta(days=1)).isoformat(),
                         price_gram=2e6, exclusion_reason=None))
        self.assertEqual(analyze_sales(rows, "word", as_of=NOW, complete=False).status, "insufficient_data")
        evidence = analyze_sales(rows, "word", as_of=NOW, complete=True)
        self.assertEqual(evidence.status, "observed")
        self.assertEqual(evidence.median_gram, 12.5)
        self.assertEqual(evidence.count, 6)
        self.assertEqual(evidence.liquidity, "thin")
        self.assertEqual(analyze_sales(rows, "word", as_of=NOW - timedelta(days=365), complete=True).status,
                         "insufficient_data")

    def test_rating_is_not_price_and_expired_evidence_falls_back(self):
        evidence = Evidence("observed", 30, 15, 1.2, "higher", "medium", "completed_sales_median")
        fresh = commercial_rating("amber", 80, evidence, as_of=NOW, observed_at=NOW)
        self.assertEqual(fresh["status"], "observed")
        self.assertLessEqual(fresh["score"], 100)
        self.assertEqual(commercial_rating("amber", 80, evidence, as_of=NOW,
                                           observed_at=NOW - timedelta(days=31))["score"], None)
        self.assertEqual(commercial_rating("telegramofficial", 80, evidence, as_of=NOW,
                                           observed_at=NOW)["status"], "excluded")
        self.assertIsNone(commercial_rating("amber", 80, Evidence("insufficient_data"),
                                             as_of=NOW, observed_at=NOW)["score"])
        self.assertIsNone(commercial_rating("amber", 80, Evidence("observed", 5, 12, 1, "thin", "low"),
                                             as_of=NOW, observed_at=NOW)["score"])

    def test_same_pool_compare_readonly_and_no_future_leak(self):
        with Store(self.path, clock=lambda: NOW) as store:
            pool = CandidatePool(store)
            pool.generate(20, seed=42, pool_size=20)
            with sqlite3.connect(self.path) as db:
                persist_test_pool(db, 40, 42, clock=lambda: NOW)
                import_daily(db, feed(), now=NOW)
                before = db.execute("SELECT COUNT(*) FROM state").fetchone()[0]
                self.assertEqual(score_unchecked(db, clock=lambda: NOW), 20)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM state").fetchone()[0], before)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM market_candidate_scores").fetchone()[0], 20)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM market_candidate_scores WHERE commercial_score IS NOT NULL"
                                            ).fetchone()[0], 0)
                result = compare(db, as_of=NOW, top=20)
                self.assertEqual(result["count"], 40)
                self.assertEqual(result["valuation"], "insufficient_data")
                self.assertEqual(result["observed_sales_sample"]["count_5plus"], 1)
                self.assertTrue(result["v2"]["top"][0]["reason"])
                self.assertEqual(result["v1"]["top"], result["v2"]["top"])
                historic = compare(db, as_of=NOW - timedelta(days=1), top=20)
                self.assertEqual(historic["status"], "no_candidates_at_cutoff")
                # Move only the synthetic test pool's creation time backward:
                # market data fetched later must still be unavailable.
                db.execute("UPDATE market_test_candidates SET generated_at=?",
                           ((NOW - timedelta(days=2)).isoformat(),))
                historic = compare(db, as_of=NOW - timedelta(days=1), top=20)
                self.assertEqual(historic["cache_status"], "unavailable_at_cutoff")
                self.assertTrue(all(x["commercial_score"] is None for x in historic["v2"]["top"]))
                self.assertEqual(explain(db, result["v2"]["top"][0]["username"], as_of=NOW)["source"],
                                 "Data: TG Gifts (https://tggifts.com)")

    def test_runtime_optin_and_stale_fallback_preserves_quota(self):
        with Store(self.path, clock=lambda: NOW) as store:
            pool = CandidatePool(store)
            pool.generate(20, seed=42, pool_size=20)
            initial = pool.pick([], 12)
            with store.db:
                store.db.execute("""CREATE TABLE market_candidate_scores (
                    username TEXT PRIMARY KEY, commercial_score INTEGER, status TEXT,
                    confidence TEXT, reason TEXT,source TEXT,obtained_at TEXT,scored_at TEXT)""")
                alternative = next(x[0] for x in store.db.execute(
                    "SELECT username FROM discovery_candidates WHERE username!=? LIMIT 1", (initial,)))
                store.db.execute("INSERT INTO market_candidate_scores VALUES (?,?,?,?,?,?,?,?)",
                                 (alternative, 100, "insufficient_data", "none", "heuristic_only", "fixture",
                                  NOW.isoformat(), NOW.isoformat()))
            self.assertEqual(pool.pick([], 12), initial)
            self.assertEqual(pool.pick([], 12, market=True), initial)
            with store.db:
                store.db.execute("UPDATE market_candidate_scores SET status='observed',confidence='medium'"
                                 " WHERE username=?", (alternative,))
            self.assertEqual(pool.pick([], 12, market=True), alternative)
            self.assertEqual(pool.remaining(12), 12)
            with store.db:
                store.db.execute("UPDATE market_candidate_scores SET obtained_at=? WHERE username=?",
                                 ((NOW - timedelta(days=31)).isoformat(), alternative))
            self.assertEqual(pool.pick([], 12, market=True), initial)

    def test_promotion_dry_run_atomic_history_and_capacity(self):
        with Store(self.path, clock=lambda: NOW) as store:
            pool = CandidatePool(store)
            pool.generate(10, seed=42, pool_size=10)
            persist_test_pool(store.db, 40, 42, clock=lambda: NOW)
            existing = store.db.execute("SELECT username FROM market_test_candidates LIMIT 1").fetchone()[0]
            with store.db:
                store.db.execute("""INSERT INTO discovery_candidates
                    (username,category,score,reason,generated_at,status,last_checked_at,attempts)
                    VALUES (?,'startup',30,'historical',?,'confirmed_free',?,3)""",
                    (existing, NOW.isoformat(), NOW.isoformat()))
            store.record(existing, "free", alert_first_free=True)
            before = {table: list(store.db.execute(f"SELECT * FROM {table}")) for table in
                      ("state", "changes", "notification_outbox", "discovery_attempts")}
            old = store.db.execute("SELECT * FROM discovery_candidates WHERE username=?", (existing,)).fetchone()
            plan = promotion_plan(store.db, count=20, pool_size=30, manual=["amber"])
            self.assertEqual(plan["new_count"], 20)
            self.assertNotIn(existing, [r["username"] for r in plan["candidates"]])
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM discovery_candidates").fetchone()[0], 11)
            result = promote(store.db, count=20, pool_size=30, manual=["amber"], clock=lambda: NOW)
            self.assertEqual(result, plan)
            self.assertEqual(store.db.execute("SELECT * FROM discovery_candidates WHERE username=?",
                                              (existing,)).fetchone(), old)
            self.assertEqual({table: list(store.db.execute(f"SELECT * FROM {table}")) for table in before}, before)
            self.assertTrue(all(source == "market_test" and kind and reason for source, kind, reason in
                                store.db.execute("SELECT origin,market_class,promotion_reason FROM discovery_candidates"
                                                 " WHERE origin='market_test'")))
            promote(store.db, count=20, pool_size=30, clock=lambda: NOW)
            self.assertEqual(store.db.execute("SELECT COUNT(DISTINCT username) FROM discovery_candidates"
                                              ).fetchone()[0], store.db.execute(
                "SELECT COUNT(*) FROM discovery_candidates").fetchone()[0])
            self.assertLessEqual(store.db.execute("SELECT COUNT(*) FROM discovery_candidates WHERE status='queued'"
                                               ).fetchone()[0], 30)
            self.assertEqual(store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_full_pool_replaces_only_never_started_rows(self):
        with Store(self.path, clock=lambda: NOW) as store:
            pool = CandidatePool(store)
            pool.generate(300, seed=42, pool_size=300)
            persist_test_pool(store.db, 40, 42, clock=lambda: NOW)
            old = pool.pick([], 12)
            self.assertTrue(pool.begin(old, 12))
            pool.temporary_error(old, "timeout", 600)
            old_row = store.db.execute("SELECT * FROM discovery_candidates WHERE username=?", (old,)).fetchone()
            plan = promotion_plan(store.db, count=10, pool_size=300)
            self.assertEqual(plan["new_count"], 10)
            self.assertEqual(len(plan["replaced"]), 10)
            self.assertNotIn(old, [row["username"] for row in plan["replaced"]])
            promote(store.db, count=10, pool_size=300, clock=lambda: NOW)
            self.assertEqual(store.db.execute("SELECT * FROM discovery_candidates WHERE username=?",
                                              (old,)).fetchone(), old_row)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM discovery_candidates WHERE status='queued' OR status='retry'"
                                              ).fetchone()[0], 300)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM discovery_candidates WHERE excluded_reason='market_promotion_replacement'"
                                              ).fetchone()[0], 10)
            self.assertEqual(store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_interval_and_rolling_quota_after_restart(self):
        with Store(self.path, clock=lambda: NOW) as store:
            pool = CandidatePool(store)
            pool.generate(12, seed=42, pool_size=12)
            name = pool.pick([], 12)
            self.assertTrue(pool.begin(name, 12))
            pool.temporary_error(name, "timeout", 600)
        clock = [NOW + timedelta(seconds=7199)]
        with Store(self.path, clock=lambda: clock[0]) as reopened:
            pool = CandidatePool(reopened)
            self.assertIsNone(pool.pick([], 12))
            self.assertFalse(pool.begin(name, 12))
            self.assertEqual(pool.remaining(12), 11)
            clock[0] += timedelta(seconds=1)
            self.assertIsNotNone(pool.pick([], 12))
            self.assertTrue(pool.begin(name, 12))
            self.assertEqual(pool.remaining(12), 10)
            self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM discovery_attempts").fetchone()[0], 2)

    def test_local_cli_no_mtproto_and_readonly_reports(self):
        manual = self.path.parent / "usernames.txt"
        manual.write_text("manual123\n")
        with patch.dict(os.environ, {"DB_PATH": str(self.path)}, clear=True), \
                patch("dotenv.load_dotenv"), \
                patch("username_monitor.__main__.load_config", side_effect=AssertionError("secret")), \
                patch("username_monitor.__main__.create_telegram_client", side_effect=AssertionError("network")):
            for argv in (["market", "generate", "--count", "40", "--seed", "42"],
                         ["market", "status"], ["market", "compare", "--top", "10"],
                         ["market", "explain", "--username-market", "amber"]):
                with patch("sys.argv", ["username_monitor", *argv]), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(), 0)
                    self.assertTrue(output.getvalue())
            with sqlite3.connect(self.path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM market_test_candidates").fetchone()[0], 40)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='notification_outbox'").fetchone()[0], 0)
        env = {"DB_PATH": str(self.path), "USERNAMES_PATH": str(manual)}
        with patch.dict(os.environ, env, clear=True), patch("dotenv.load_dotenv"), \
                patch("username_monitor.__main__.load_config", side_effect=AssertionError("secret")), \
                patch("username_monitor.__main__.create_telegram_client", side_effect=AssertionError("network")):
            with patch("sys.argv", ["username_monitor", "market", "promote", "--count", "4", "--dry-run"]), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(), 0)
                self.assertEqual(json.loads(output.getvalue())["new_count"], 4)
            with sqlite3.connect(self.path) as db:
                self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='discovery_candidates'").fetchone())
            with patch("sys.argv", ["username_monitor", "market", "promote", "--count", "4", "--apply"]), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 1)
            with patch.dict(os.environ, {**env, "MARKET_DISCOVERY_ENABLED": "true"}, clear=True), \
                    patch("sys.argv", ["username_monitor", "market", "promote", "--count", "4", "--apply"]), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(), 0)
                self.assertEqual(json.loads(output.getvalue())["new_count"], 4)
            with sqlite3.connect(self.path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM discovery_candidates").fetchone()[0], 4)


if __name__ == "__main__":
    unittest.main()
