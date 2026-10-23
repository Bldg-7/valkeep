"""실제 공개 stdio 서버와 공식 업스트림을 연결하는 테스트 실행기."""

import asyncio
import logging
import sys
from pathlib import Path

from valkeep.audit import Audit
from valkeep.config import load_policy
from valkeep.proxy import Proxy
from valkeep.server import build_server
from valkeep.transport import public_stdio
from valkeep.upstream import connect_upstream

logging.disable(logging.CRITICAL)


async def run():
    policy = load_policy(Path(sys.argv[1]))
    audit = Audit(policy)
    command = (sys.executable, "-I", str(Path(__file__).with_name("official_stub.py")))
    try:
        async with connect_upstream(policy, command=command, environment={}) as upstream:
            proxy = Proxy(policy, upstream, audit)
            server = build_server(proxy)
            try:
                async with public_stdio(policy.limits.max_request_bytes) as streams:
                    await server.run(*streams, server.create_initialization_options())
            finally:
                await proxy.close()
    finally:
        audit.close()


asyncio.run(run())
