"""공식 서버의 Redis 연결부만 대체한다. 네트워크 접근은 금지한다."""

import logging
import os
import socket
import sys
import time

logging.disable(logging.CRITICAL)


def no_network(*args, **kwargs):
    raise AssertionError("테스트 중 외부 네트워크 사용 금지")


socket.socket.connect = no_network

from src.common.connection import RedisConnectionManager  # noqa: E402
from src.main import main  # noqa: E402


class FixtureRedis:
    def type(self, key):
        if key == "safe:crash":
            os._exit(3)
        if key == "safe:slow":
            time.sleep(10)  # 테스트 대상인 동기 업스트림의 장기 실행을 재현한다.
        return "list"

    def ttl(self, key):
        return -1

    def pttl(self, key):
        return -1

    def ping(self):
        return True

    def llen(self, name):
        return int(os.environ.get("FIXTURE_LENGTH", "7"))


RedisConnectionManager.get_connection = classmethod(lambda cls, **kwargs: FixtureRedis())
print("upstream-private-secret", file=sys.stderr)
from valkeep.reader import register  # noqa: E402

register(FixtureRedis())
main()
