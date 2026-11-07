"""Optional, bounded SQLite telemetry; never shares the state/outbox transaction."""
import json
import logging
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOG = logging.getLogger("username_monitor")
RETENTION_DAYS = 14
RPC_RESULTS = ("free", "occupied", "unavailable", "fragment", "invalid",
               "timeout", "flood_wait", "rpc_error", "network_error", "other_error")
SUCCESS_RESULTS = frozenset(RPC_RESULTS[:5])


def _utc(clock):
    return clock().astimezone(timezone.utc)


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    point = (len(ordered) - 1) * percentile / 100
    low = math.floor(point)
    return round(ordered[low] + (ordered[math.ceil(point)] - ordered[low]) * (point - low), 2)


def _finished_at(started_at, duration_ms):
    return (datetime.fromisoformat(started_at) + timedelta(milliseconds=duration_ms)).isoformat(timespec="seconds")


def safe_record(telemetry, method, *args):
    if telemetry is None:
        return
    try:
        getattr(telemetry, method)(*args)
    except Exception as exc:
        LOG.warning("Не удалось записать телеметрию: %s", type(exc).__name__)


class Telemetry:
    def __init__(self, path: Path, *, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.db = None
        self._pruned_on = None
        try:
            self.db = sqlite3.connect(path)
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS rpc_metrics (
                    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
                    duration_ms REAL NOT NULL, phase TEXT NOT NULL,
                    result TEXT NOT NULL, flood_wait_seconds INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_rpc_metrics_started ON rpc_metrics(started_at);
                CREATE TABLE IF NOT EXISTS delivery_metrics (
                    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
                    duration_ms REAL NOT NULL, success INTEGER NOT NULL,
                    retry INTEGER NOT NULL, latency_ms REAL
                );
                CREATE INDEX IF NOT EXISTS idx_delivery_metrics_started ON delivery_metrics(started_at);
                CREATE TABLE IF NOT EXISTS cycle_metrics (
                    id INTEGER PRIMARY KEY, completed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cycle_metrics_completed ON cycle_metrics(completed_at);
            """)
            self._prune_if_due()
        except Exception as exc:
            LOG.warning("Телеметрия недоступна: %s", type(exc).__name__)
            self.close()

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _write(self, sql, params):
        if self.db is None:
            return
        try:
            self._prune_if_due()
            with self.db:
                self.db.execute(sql, params)
        except Exception as exc:
            try:
                self.db.rollback()
            except sqlite3.Error:
                pass
            # Only local exception type; SQLite messages can contain private paths.
            LOG.warning("Не удалось записать телеметрию: %s", type(exc).__name__)

    def _prune_if_due(self):
        today = _utc(self.clock).date()
        if self._pruned_on == today:
            return
        cutoff = (_utc(self.clock) - timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
        with self.db:
            self.db.execute("DELETE FROM rpc_metrics WHERE started_at < ?", (cutoff,))
            self.db.execute("DELETE FROM delivery_metrics WHERE started_at < ?", (cutoff,))
            self.db.execute("DELETE FROM cycle_metrics WHERE completed_at < ?", (cutoff,))
        self._pruned_on = today

    def rpc(self, started_at, duration_ms, phase, result, flood_wait_seconds=None):
        if result not in RPC_RESULTS or phase not in ("primary", "confirmation"):
            return
        self._write("INSERT INTO rpc_metrics(started_at,duration_ms,phase,result,flood_wait_seconds) "
                    "VALUES (?,?,?,?,?)", (started_at, max(0, duration_ms), phase, result, flood_wait_seconds))

    def delivery(self, started_at, duration_ms, success, retry, latency_ms=None):
        self._write("INSERT INTO delivery_metrics(started_at,duration_ms,success,retry,latency_ms) "
                    "VALUES (?,?,?,?,?)", (started_at, max(0, duration_ms), int(success), int(retry), latency_ms))

    def cycle(self):
        self._write("INSERT INTO cycle_metrics(completed_at) VALUES (?)",
                    (_utc(self.clock).isoformat(timespec="seconds"),))


def read_report(path: Path, hours: int = 24, *, clock=None) -> dict:
    """Read the existing file without a migration, journal writes, or network access."""
    if hours < 1:
        raise ValueError("--hours должен быть положительным целым числом")
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = _utc(clock)
    cutoff = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    report = {
        "hours": hours, "generated_at": now.isoformat(timespec="seconds"),
        "rpc": {"checks": 0, "primary": 0, "confirmation": 0,
                "successful": 0, "failed": 0, "results": {key: 0 for key in RPC_RESULTS},
                "p50_ms": None, "p95_ms": None, "flood_wait_count": 0,
                "max_flood_wait_seconds": None, "timeout_count": 0, "network_error_count": 0,
                "last_success_at": None},
        "cycles_completed": 0,
        "delivery": {"successful": 0, "failed": 0, "retries": 0,
                     "http_p50_ms": None, "http_p95_ms": None,
                     "average_latency_ms": None, "p95_latency_ms": None,
                     "last_success_at": None},
        "outbox": {"pending": 0, "delivered": 0, "cancelled": 0,
                   "oldest_pending_age_seconds": None, "max_pending_attempts": None},
    }
    if not path.exists():
        return report
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "rpc_metrics" in tables:
            durations = []
            for started, duration, phase, result, flood in db.execute(
                    "SELECT started_at,duration_ms,phase,result,flood_wait_seconds FROM rpc_metrics "
                    "WHERE started_at >= ?", (cutoff,)):
                if result not in report["rpc"]["results"]:
                    continue
                r = report["rpc"]
                r["checks"] += 1
                r[phase] += 1
                r["results"][result] += 1
                r["successful" if result in SUCCESS_RESULTS else "failed"] += 1
                durations.append(duration)
                if result == "flood_wait":
                    r["flood_wait_count"] += 1
                    if flood is not None:
                        r["max_flood_wait_seconds"] = max(r["max_flood_wait_seconds"] or 0, flood)
                if result == "timeout":
                    r["timeout_count"] += 1
                if result == "network_error":
                    r["network_error_count"] += 1
                if result in SUCCESS_RESULTS:
                    finished = _finished_at(started, duration)
                    r["last_success_at"] = max(r["last_success_at"] or finished, finished)
            report["rpc"]["p50_ms"] = _percentile(durations, 50)
            report["rpc"]["p95_ms"] = _percentile(durations, 95)
        if "delivery_metrics" in tables:
            durations, latencies = [], []
            for started, duration, success, retry, latency in db.execute(
                    "SELECT started_at,duration_ms,success,retry,latency_ms FROM delivery_metrics "
                    "WHERE started_at >= ?", (cutoff,)):
                d = report["delivery"]
                d["successful" if success else "failed"] += 1
                d["retries"] += int(retry)
                durations.append(duration)
                if success:
                    finished = _finished_at(started, duration)
                    d["last_success_at"] = max(d["last_success_at"] or finished, finished)
                    if latency is not None:
                        latencies.append(latency)
            d = report["delivery"]
            d["http_p50_ms"] = _percentile(durations, 50)
            d["http_p95_ms"] = _percentile(durations, 95)
            d["average_latency_ms"] = round(sum(latencies) / len(latencies), 2) if latencies else None
            d["p95_latency_ms"] = _percentile(latencies, 95)
        if "cycle_metrics" in tables:
            report["cycles_completed"] = db.execute(
                "SELECT COUNT(*) FROM cycle_metrics WHERE completed_at >= ?", (cutoff,)).fetchone()[0]
        if "notification_outbox" in tables:
            report["outbox"].update(db.execute(
                "SELECT status,COUNT(*) FROM notification_outbox GROUP BY status").fetchall())
            oldest, attempts = db.execute(
                "SELECT MIN(created_at),MAX(attempts) FROM notification_outbox WHERE status='pending'").fetchone()
            if oldest is not None:
                report["outbox"]["oldest_pending_age_seconds"] = max(0, int(
                    (now - datetime.fromisoformat(oldest)).total_seconds()))
                report["outbox"]["max_pending_attempts"] = attempts
    finally:
        db.close()
    return report


def show_report(path: Path, hours: int, as_json: bool):
    report = read_report(path, hours)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return
    rpc, delivery, outbox = report["rpc"], report["delivery"], report["outbox"]
    display = lambda value: "no data" if value is None else str(value)
    print(f"Период: {hours} ч; проверки: {rpc['checks']} (первичные {rpc['primary']}, подтверждения {rpc['confirmation']})")
    print(f"RPC успешно/ошибки: {rpc['successful']}/{rpc['failed']}; результаты: {rpc['results']}")
    print(f"RPC P50/P95 мс: {display(rpc['p50_ms'])}/{display(rpc['p95_ms'])}; "
          f"FloodWait: {rpc['flood_wait_count']}, максимум с: {display(rpc['max_flood_wait_seconds'])}; "
          f"timeout/network: {rpc['timeout_count']}/{rpc['network_error_count']}")
    print(f"Завершённых циклов: {report['cycles_completed']}; последний успешный RPC: {display(rpc['last_success_at'])}")
    print(f"Доставка успешно/ошибки/повторы: {delivery['successful']}/{delivery['failed']}/{delivery['retries']}; "
          f"HTTP P50/P95 мс: {display(delivery['http_p50_ms'])}/{display(delivery['http_p95_ms'])}")
    print(f"Задержка доставки средняя/P95 мс: {display(delivery['average_latency_ms'])}/{display(delivery['p95_latency_ms'])}; "
          f"последняя успешная доставка: {display(delivery['last_success_at'])}")
    print(f"Outbox pending/delivered/cancelled: {outbox['pending']}/{outbox['delivered']}/{outbox['cancelled']}; "
          f"старейшее pending с: {display(outbox['oldest_pending_age_seconds'])}; "
          f"максимум попыток: {display(outbox['max_pending_attempts'])}")
