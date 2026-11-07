"""Durable Bot API delivery; never opens a Telegram MTProto client."""
import asyncio
from datetime import timezone
import logging
import time

from .names import load_names
from . import notify as notify_module
from .store import Store
from .telemetry import safe_record

LOG = logging.getLogger("username_monitor")
BATCH_SIZE = 10


async def deliver_due(config, store: Store, telemetry=None) -> int:
    names, _ = load_names(config.usernames_path)
    store.cancel_unwatched(names)
    processed = 0
    for event in store.due_notifications(BATCH_SIZE):
        if not store.is_current_free(event):
            store.cancel_notification(event.id)
            continue
        message = (f"🟢 Возможный свободный username: @{event.username}\n"
                   f"До этого: {event.previous_status or 'не проверялся'}\n"
                   "Проверь и назначь вручную в Telegram. Доступность не гарантирована.")
        attempt = notify_module.HttpAttempt() if telemetry is not None else None
        notify_options = {"bot_proxy_url": config.bot_proxy_url,
                          "bot_proxy_secret": config.bot_proxy_secret}
        if attempt is not None:
            notify_options["attempt"] = attempt
        try:
            delivered = await notify_module.notify(config.bot_token, config.chat_id, message,
                                                   **notify_options)
        except Exception:
            # Do not persist or print exception text, URLs or response bodies.
            delivered = False
        if delivered:
            store.delivered(event.id)
        else:
            store.failed(event)
            LOG.warning("@%s: уведомление не доставлено", event.username)
        if attempt is not None and attempt.started_at is not None and attempt.duration_ms is not None:
            latency = None
            if delivered:
                # The state transition is durable before this optional statistic.
                try:
                    created = store.db.execute("SELECT created_at FROM notification_outbox WHERE id=?",
                                               (event.id,)).fetchone()[0]
                    from datetime import datetime
                    latency = max(0, (store.clock().astimezone(timezone.utc) -
                                      datetime.fromisoformat(created)).total_seconds() * 1000)
                except Exception:
                    # Metrics never roll back a delivered outbox event.
                    pass
            safe_record(telemetry, "delivery", attempt.started_at, attempt.duration_ms,
                        delivered, event.attempts > 0, latency)
        processed += 1
    return processed


async def wait_with_outbox(config, store: Store, pause: float, *,
                           sleep=asyncio.sleep, monotonic=time.monotonic, telemetry=None) -> None:
    deadline = monotonic() + pause
    while True:
        await deliver_due(config, store, telemetry)
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        next_at = store.next_notification_at()
        if next_at is None:
            await sleep(remaining)
        else:
            until_due = (next_at - store.clock().astimezone(timezone.utc)).total_seconds()
            # A nonzero wait prevents a fast loop if a due batch is backed up.
            await sleep(min(remaining, max(0.1, until_due)))
