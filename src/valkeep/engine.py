"""인증 후 HELLO 응답으로 엔진·버전·standalone 계약을 검증한다."""

import re

from valkey.exceptions import ConnectionError

from valkeep.errors import Code

MINIMUM_VERSIONS = {"valkey": (8, 1), "redis": (7, 2)}


class ConnectionPolicyError(ConnectionError):
    def __init__(self, code: Code):
        self.code = code
        super().__init__(code.value)


def initialize_connection(connection, expected_engine: str) -> None:
    # 기본 인증·TLS·DB 선택은 공식 client 구현을 사용한다.
    connection.on_connect()
    connection.send_command("HELLO", 2, check_health=False)
    response = connection.read_response()
    try:
        if isinstance(response, list):
            if len(response) % 2 or len(set(response[::2])) != len(response) // 2:
                raise ValueError
            response = dict(zip(response[::2], response[1::2], strict=True))
        if not isinstance(response, dict):
            raise ValueError
        info = {
            (key.decode("ascii") if isinstance(key, bytes) else key): (
                value.decode("ascii") if isinstance(value, bytes) else value
            )
            for key, value in response.items()
        }
        if info.get("server") != expected_engine:
            raise ConnectionPolicyError(Code.ENGINE_MISMATCH)
        if info.get("mode") != "standalone" or info.get("proto") != 2:
            raise ConnectionPolicyError(Code.UNSUPPORTED_TOPOLOGY)
        version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", info.get("version", ""))
        if not version:
            raise ValueError
        if tuple(map(int, version.groups()[:2])) < MINIMUM_VERSIONS[expected_engine]:
            raise ConnectionPolicyError(Code.UNSUPPORTED_SERVER_VERSION)
    except (ValueError, TypeError, UnicodeError, KeyError):
        raise ConnectionPolicyError(Code.UPSTREAM_CONTRACT_MISMATCH) from None
