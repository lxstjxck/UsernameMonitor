"""Bind Telethon's DC identity to a separate FakeTLS MTProxy TCP endpoint."""

from tgnet.connection.faketls import TgNetConnectionTls


def connection_for_mtproxy(host: str, port: int, secret: bytes):
    # A class, not a connection instance: Telethon instantiates it for each
    # connect/reconnect and passes the current DC ID and logger bundle.
    class FakeTlsConnection(TgNetConnectionTls):
        def __init__(self, ip, dc_port, dc_id, *, loggers, proxy=None, local_addr=None):
            if proxy is not None:
                raise ValueError("FakeTLS не использует SOCKS5 proxy")
            super().__init__(
                host, port, dc_id, loggers=loggers, local_addr=local_addr,
                secret=secret,
            )
            self.telegram_dc_ip = ip
            self.telegram_dc_port = dc_port

        def __str__(self):
            return "FakeTLS MTProxy connection"

    return FakeTlsConnection
