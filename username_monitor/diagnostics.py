"""Read-only MTProto probes with bounded waits and redacted output."""
import asyncio
import logging
from pathlib import Path
from time import perf_counter

from telethon import errors, functions


CONNECT_TIMEOUT_SECONDS = 20
AUTH_TIMEOUT_SECONDS = 15
CONFIG_TIMEOUT_SECONDS = 15
RESOLVE_TIMEOUT_SECONDS = 10
DISCONNECT_TIMEOUT_SECONDS = 5

USERNAME_RESPONSES = (
    errors.UsernameInvalidError,
    errors.UsernameOccupiedError,
    errors.UsernamePurchaseAvailableError,
)
RESOLVE_RESPONSES = (errors.UsernameNotOccupiedError, errors.UsernameInvalidError)


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "нет ответа в пределах тайм-аута"
    if isinstance(exc, errors.FloodWaitError):
        return f"Telegram требует паузу {exc.seconds} с"
    if isinstance(exc, errors.UsernameNotOccupiedError):
        return "имя не разрешено: username не занят"
    if isinstance(exc, errors.UsernameInvalidError):
        return "Telegram отклонил username"
    if isinstance(exc, errors.UsernameOccupiedError):
        return "username занят"
    if isinstance(exc, errors.UsernamePurchaseAvailableError):
        return "username доступен через Fragment"
    if isinstance(exc, errors.RPCError):
        return "Telegram вернул RPC-ошибку"
    if isinstance(exc, (OSError, ConnectionError)):
        return "ошибка соединения"
    return "операция завершилась ошибкой"


async def _stage(label, operation, timeout, describe, accepted_errors=()):
    started = perf_counter()
    try:
        result = await asyncio.wait_for(operation(), timeout)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        elapsed = perf_counter() - started
        print(f"{label}: {elapsed:.2f} с; {type(exc).__name__}: {_safe_error(exc)}")
        return isinstance(exc, accepted_errors), exc
    elapsed = perf_counter() - started
    print(f"{label}: {elapsed:.2f} с; {describe(result)}")
    return True, result


async def diagnose(config, username: str, create_client) -> int:
    """Compare read-only Telegram RPCs using the existing session."""
    session_file = Path(str(config.session_path))
    if session_file.suffix != ".session":
        session_file = Path(f"{session_file}.session")
    if not session_file.is_file():
        print("session: FileNotFoundError: файл сессии не найден")
        return 1

    # Telethon may log raw exception details. Diagnostics only prints fixed summaries.
    previous_logging_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        try:
            client = create_client(config, flood_sleep_threshold=0)
        except Exception as exc:
            print(f"client: {type(exc).__name__}: {_safe_error(exc)}")
            return 1

        result_code = 1
        try:
            connected, _ = await _stage("connect", client.connect,
                                        CONNECT_TIMEOUT_SECONDS, lambda _: "подключено")
            if connected:
                authorized, auth_value = await _stage(
                    "is_user_authorized", client.is_user_authorized,
                    AUTH_TIMEOUT_SECONDS,
                    lambda value: "авторизована" if value else "не авторизована",
                )
                config_ok, config_result = await _stage(
                    "help.GetConfigRequest", lambda: client(functions.help.GetConfigRequest()),
                    CONFIG_TIMEOUT_SECONDS, lambda _: "ответ получен",
                )
                if not isinstance(config_result, errors.FloodWaitError):
                    resolve_ok, resolve_result = await _stage(
                        "contacts.ResolveUsernameRequest",
                        lambda: client(functions.contacts.ResolveUsernameRequest(username=username)),
                        RESOLVE_TIMEOUT_SECONDS, lambda _: "имя успешно разрешено",
                        accepted_errors=RESOLVE_RESPONSES,
                    )
                    if not isinstance(resolve_result, errors.FloodWaitError):
                        username_ok, _ = await _stage(
                            "account.CheckUsernameRequest",
                            lambda: client(functions.account.CheckUsernameRequest(username=username)),
                            config.check_timeout_seconds,
                            lambda value: "ответ BoolTrue" if bool(value) else "ответ BoolFalse",
                            accepted_errors=USERNAME_RESPONSES,
                        )
                        result_code = 0 if authorized and auth_value and config_ok and resolve_ok and username_ok else 1
        finally:
            disconnected, _ = await _stage("disconnect", client.disconnect, DISCONNECT_TIMEOUT_SECONDS,
                                           lambda _: "отключено")
        return result_code if disconnected else 1
    finally:
        logging.disable(previous_logging_level)
