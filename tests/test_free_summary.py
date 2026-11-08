"""TOP-10 summary reads confirmed SQLite state and preserves Outbox semantics."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from username_monitor.discovery import CandidatePool
from username_monitor.free_summary import format_top_confirmed_free, top_confirmed_free
from username_monitor.outbox import deliver_due
from username_monitor.store import Store


class TopConfirmedFreeCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "monitor.sqlite3"
        self.names = Path(tmp.name) / "usernames.txt"
        self.names.write_text("")
        self.config = SimpleNamespace(
            usernames_path=self.names, auto_discovery_enabled=True,
            bot_token="synthetic", chat_id="synthetic",
            bot_proxy_url=None, bot_proxy_secret=None,
        )

    def add_candidate(self, store, name, score, *,
                      candidate_status="confirmed_free", result="free",
                      state_status="free", notify=False):
        with store.db:
            store.db.execute("""
                INSERT INTO discovery_candidates
                   (username,category,score,reason,generated_at,status,
                    last_checked_at,result,attempts,origin,market_class)
                VALUES (?, 'startup', ?, 'test', ?, ?, ?, ?, 1, 'market_test', 'brand')
            """, (name, score, store.now(), candidate_status, store.now(), result))
        store.record(name, state_status, alert_first_free=notify,
                     notification_source="auto" if notify else "manual")

    async def test_top_ranking_cap_and_current_free_filter(self):
        with Store(self.db_path) as store:
            CandidatePool(store)
            self.assertEqual(top_confirmed_free(store.db), [])
            for n in range(12):
                self.add_candidate(store, f"testname{n:02}", 50 + n)
            self.add_candidate(store, "notconfirmed", 99, candidate_status="queued")
            self.add_candidate(store, "notfree", 99, result="occupied",
                               state_status="occupied")
            self.add_candidate(store, "statechanged", 99, state_status="occupied")
            rows = top_confirmed_free(store.db)
            self.assertEqual(len(rows), 10)
            self.assertEqual([row[0] for row in rows],
                             [f"testname{n:02}" for n in range(11, 1, -1)])
            message = format_top_confirmed_free(store.db)
            self.assertIn("ТОП-10 подтверждённых free (10/10)", message)
            self.assertIn("@testname11", message)
            self.assertNotIn("@testname00", message)
            self.assertNotIn("@notconfirmed", message)
            self.assertNotIn("@statechanged", message)
            self.assertIn("UTC", message)
            self.assertEqual(store.db.execute(
                "SELECT COUNT(*) FROM discovery_attempts").fetchone()[0], 0)
            with self.assertRaises(ValueError):
                top_confirmed_free(store.db, 11)

    async def test_one_auto_alert_contains_top_without_extra_rpc(self):
        with Store(self.db_path) as store:
            CandidatePool(store)
            self.add_candidate(store, "premiumname", 93)
            self.add_candidate(store, "newfree", 88, notify=True)
            with patch("username_monitor.outbox.notify_module.notify",
                       new_callable=AsyncMock, return_value=True) as notify, \
                 patch("username_monitor.__main__.create_telegram_client",
                       side_effect=AssertionError("No MTProto")):
                self.assertEqual(await deliver_due(self.config, store), 1)
                notify.assert_awaited_once()
                message = notify.await_args.args[2]
                self.assertIn("🟢 Возможный свободный username: @newfree", message)
                self.assertIn("ТОП-10 подтверждённых free (2/10)", message)
                self.assertLess(message.index("@premiumname"), message.rindex("@newfree"))
                self.assertEqual(await deliver_due(self.config, store), 0)
                notify.assert_awaited_once()
            self.assertEqual(store.db.execute(
                "SELECT COUNT(*) FROM discovery_attempts").fetchone()[0], 0)
            self.assertEqual(store.db.execute(
                "SELECT status FROM notification_outbox").fetchone()[0], "delivered")

    async def test_failed_delivery_preserves_pending_and_no_rpc(self):
        with Store(self.db_path) as store:
            CandidatePool(store)
            self.add_candidate(store, "retryname", 91, notify=True)
            with patch("username_monitor.outbox.notify_module.notify",
                       new_callable=AsyncMock, return_value=False) as notify:
                self.assertEqual(await deliver_due(self.config, store), 1)
                notify.assert_awaited_once()
            self.assertEqual(store.db.execute(
                "SELECT status,attempts FROM notification_outbox").fetchone(),
                ("pending", 1))
            self.assertEqual(store.db.execute(
                "SELECT COUNT(*) FROM discovery_attempts").fetchone()[0], 0)
            self.assertEqual(store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    async def test_legacy_empty_and_manual_notification_unchanged(self):
        with Store(self.db_path) as store:
            self.assertEqual(top_confirmed_free(store.db), [])
            store.record("manualname", "free", alert_first_free=True)
            self.names.write_text("manualname\n")
            with patch("username_monitor.outbox.notify_module.notify",
                       new_callable=AsyncMock, return_value=True) as notify:
                self.assertEqual(await deliver_due(self.config, store), 1)
                message = notify.await_args.args[2]
                self.assertNotIn("ТОП-10", message)
                self.assertIn("Проверь и назначь вручную", message)


if __name__ == "__main__":
    unittest.main()
