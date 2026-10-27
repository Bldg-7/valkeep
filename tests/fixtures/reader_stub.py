"""전용 읽기 worker의 연결부만 대체한다. 네트워크 접근은 금지한다."""

import logging
import os
import socket
import sys
import time

logging.disable(logging.CRITICAL)


def no_network(*args, **kwargs):
    raise AssertionError("테스트 중 외부 네트워크 사용 금지")


socket.socket.connect = no_network

from valkeep.reader import Reader  # noqa: E402
from valkeep.worker import build_worker  # noqa: E402


class FixtureValkey:
    def type(self, key):
        if key == "safe:crash":
            os._exit(3)
        if key == "safe:slow":
            time.sleep(10)  # 동기 worker의 장기 실행을 재현한다.
        return "list"

    def pttl(self, key):
        return -1

    def ping(self):
        return True

    def llen(self, name):
        return int(os.environ.get("FIXTURE_LENGTH", "7"))


worker = build_worker(Reader(FixtureValkey()))


@worker.tool()
async def fixture_unapproved() -> str:
    """추가된 업스트림 도구가 공개되지 않는지 검사하기 위한 도구."""
    return "must-not-be-called"


print("upstream-private-secret", file=sys.stderr)
worker.run()
