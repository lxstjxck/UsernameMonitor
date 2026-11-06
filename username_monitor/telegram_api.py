"""MTProto availability checks. This module never claims or changes usernames."""
from telethon import functions, errors


async def check_username(client, name: str) -> tuple[str, str]:
    try:
        # BoolTrue => available for current account. It is not a reservation.
        value = await client(functions.account.CheckUsernameRequest(username=name))
        return ("free", "") if bool(value) else ("unavailable", "Telegram вернул BoolFalse")
    except errors.FloodWaitError:
        raise
    except errors.UsernameInvalidError:
        return "invalid", "USERNAME_INVALID"
    except errors.UsernameOccupiedError:
        return "occupied", "USERNAME_OCCUPIED"
    except errors.UsernamePurchaseAvailableError:
        return "fragment", "USERNAME_PURCHASE_AVAILABLE"
    except errors.RPCError:
        raise  # Unknown RPC failures are not availability results.
