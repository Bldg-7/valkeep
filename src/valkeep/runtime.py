"""대상별 별도 태스크에서 MCP 세션 수명과 자식 프로세스를 소유한다."""

import asyncio
from contextlib import asynccontextmanager

from valkeep.audit import Audit
from valkeep.config import Policy, ServerConfig, upstream_environment
from valkeep.errors import Code, Rejected
from valkeep.proxy import Proxy
from valkeep.router import Router
from valkeep.upstream import connect_upstream


class TargetRuntime:
    def __init__(self, policy: Policy, connector):
        self.policy = policy
        self.connector = connector
        self.ready = asyncio.get_running_loop().create_future()
        self.stop_event = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        audit = None
        proxy = None
        try:
            audit = Audit(self.policy)
            async with self.connector(self.policy) as upstream:
                proxy = Proxy(self.policy, upstream, audit)
                audit.emit(
                    "startup", "-", "-", "CONTRACT_VERIFIED", ignored_tools=upstream.extra_tools
                )
                self.ready.set_result(proxy)
                try:
                    await self.stop_event.wait()
                finally:
                    await proxy.close()
        except BaseException:
            if not self.ready.done():
                self.ready.set_exception(Rejected(Code.UPSTREAM_UNAVAILABLE))
            if proxy is not None:
                proxy.closed = True
        finally:
            if audit is not None:
                audit.close()

    async def close(self):
        self.stop_event.set()
        if not self.ready.done():
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        # 초기화 도중 호출자가 취소되어도 Future 예외를 회수한다.
        if self.ready.done() and not self.ready.cancelled():
            self.ready.exception()


@asynccontextmanager
async def open_router(config: ServerConfig, *, connector=connect_upstream):
    if connector is connect_upstream:
        # 일부만 실행한 뒤 자격증명 누락을 발견하지 않도록 모두 사전 확인한다.
        for target in config.targets:
            upstream_environment(target.as_policy())
    audit = Audit(config)
    runtimes = []
    try:
        runtimes = [TargetRuntime(target.as_policy(), connector) for target in config.targets]
        results = await asyncio.gather(
            *(asyncio.shield(runtime.ready) for runtime in runtimes), return_exceptions=True
        )
        if any(isinstance(result, BaseException) for result in results):
            raise Rejected(Code.UPSTREAM_UNAVAILABLE)
        proxies = {target.id: proxy for target, proxy in zip(config.targets, results, strict=True)}
        yield Router(config, proxies, audit)
    finally:
        await asyncio.gather(*(runtime.close() for runtime in runtimes))
        audit.close()
