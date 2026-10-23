"""단일 이벤트 루프의 대기열 없는 실행 제한."""

import time
from collections.abc import Callable

from valkeep.config import Limits
from valkeep.errors import Code, Rejected


class Limiter:
    def __init__(self, limits: Limits, clock: Callable[[], float] = time.monotonic):
        self.limits = limits
        self.clock = clock
        self.tokens = 1.0
        self.last = clock()
        self.inflight = 0

    def acquire(self) -> None:
        now = self.clock()
        self.tokens = min(
            self.limits.burst,
            self.tokens + max(0, now - self.last) * self.limits.requests_per_second,
        )
        self.last = now
        if self.inflight >= self.limits.max_inflight:
            raise Rejected(Code.BUSY)
        if self.tokens < 1:
            raise Rejected(Code.RATE_LIMITED)
        self.tokens -= 1
        self.inflight += 1

    def release(self) -> None:
        if self.inflight <= 0:
            raise RuntimeError("실행 슬롯이 이미 반환되었습니다")
        self.inflight -= 1
