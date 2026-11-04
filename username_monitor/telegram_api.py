"""MTProto availability checks. This module never claims or changes usernames."""
from telethon import functions, errors


async def check_username(client, name: str) -> tuple[str, str]:
    try:
        # BoolTrue => available for current account. It is not a reservation.
        value = await client(functions.account.CheckUsernameRequest(username=name))
        return ("free", "") if bool(value) else ("unavailable", "Telegram вернул BoolFalse")
    except errors.FloodWaitError:
        raise
    except errors.RPCError as exc:
        code = str(getattr(exc, "message", "")).upper()
        mapping = {
            "USERNAME_OCCUPIED": "occupied",
            "USERNAME_PURCHASE_AVAILABLE": "fragment",
            "USERNAME_INVALID": "invalid",
        }
        for marker, status in mapping.items():
            if marker in code:
                return status, code
        raise  # Do not misreport API/network failures as 'occupied'.
