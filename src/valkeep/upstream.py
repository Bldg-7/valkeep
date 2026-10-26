"""공식 Redis MCP를 격리된 환경의 자식 프로세스로 관리한다."""

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp import ClientSession, types

from valkeep.config import Policy, upstream_environment
from valkeep.contracts import verify_contracts
from valkeep.errors import Code, Rejected
from valkeep.transport import message_streams

BOOTSTRAP = """
import logging
from importlib.metadata import version
assert version("redis-mcp-server") == "0.5.1"
assert version("mcp") == "1.30.0"
logging.disable(logging.CRITICAL)
from src.main import main
from valkeep.reader import register
register()
main()
"""


class Upstream:
    def __init__(self, process: asyncio.subprocess.Process, session: ClientSession):
        self.process = process
        self.session = session
        self.failed = False
        self.extra_tools = 0
        self._stop_lock = asyncio.Lock()

    async def call(self, name: str, arguments: dict) -> types.CallToolResult:
        if self.failed or self.process.returncode is not None:
            raise Rejected(Code.UPSTREAM_UNAVAILABLE)
        try:
            return await self.session.call_tool(name, arguments)
        except Exception:
            self.failed = True
            raise Rejected(Code.UPSTREAM_UNAVAILABLE) from None

    async def stop(self) -> None:
        async with self._stop_lock:
            self.failed = True
            if self.process.returncode is not None:
                return
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                async with asyncio.timeout(1):
                    await self.process.wait()
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()


@asynccontextmanager
async def connect_upstream(
    policy: Policy,
    *,
    command: tuple[str, ...] | None = None,
    environment: dict[str, str] | None = None,
) -> AsyncIterator[Upstream]:
    # command/environment 대체는 테스트 주입용이며 공개 설정·도구로 제공하지 않는다.
    argv = command or (sys.executable, "-I", "-c", BOOTSTRAP)
    env = environment if environment is not None else upstream_environment(policy)
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        cwd="/",
        start_new_session=True,
        limit=policy.limits.max_upstream_frame_bytes,
    )
    upstream = None
    try:
        assert process.stdout is not None and process.stdin is not None
        async with message_streams(
            process.stdout,
            process.stdin,
            policy.limits.max_upstream_frame_bytes,
            policy.limits.max_request_bytes + 4096,
        ) as (incoming, outgoing):
            async with ClientSession(incoming, outgoing) as session:
                upstream = Upstream(process, session)
                try:
                    async with asyncio.timeout(policy.limits.startup_timeout_seconds):
                        await session.initialize()
                        result = await session.list_tools()
                        if result.nextCursor is not None:
                            raise Rejected(Code.UPSTREAM_CONTRACT_MISMATCH)
                        upstream.extra_tools = verify_contracts(result.tools, policy.tools)
                except Exception:
                    await upstream.stop()
                    raise Rejected(Code.UPSTREAM_CONTRACT_MISMATCH) from None
                try:
                    yield upstream
                finally:
                    await upstream.stop()
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
