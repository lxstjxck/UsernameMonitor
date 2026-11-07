"""Durable Bot API delivery; never opens a Telegram MTProto client."""
import asyncio
from datetime import timezone
import logging
import time

from .names import load_names
from . import notify as notify_module
from .store import Store

LOG = logging.getLogger("username_monitor")
BATCH_SIZE = 10


async def deliver_due(config, store: Store) -> int:
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
        try:
            delivered = await notify_module.notify(config.bot_token, config.chat_id, message,
                                     bot_proxy_url=config.bot_proxy_url,
                                     bot_proxy_secret=config.bot_proxy_secret)
        except Exception:
            # Do not persist or print exception text, URLs or response bodies.
            delivered = False
        if delivered:
            store.delivered(event.id)
        else:
            store.failed(event)
            LOG.warning("@%s: уведомление не доставлено", event.username)
        processed += 1
    return processed


async def wait_with_outbox(config, store: Store, pause: float, *,
                           sleep=asyncio.sleep, monotonic=time.monotonic) -> None:
    deadline = monotonic() + pause
    while True:
        await deliver_due(config, store)
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
