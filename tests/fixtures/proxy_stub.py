"""다중 target stdio 서버와 테스트 읽기 worker를 연결하는 실행기."""

import asyncio
import logging
import sys
from pathlib import Path

from valkeep.config import load_config
from valkeep.runtime import open_router
from valkeep.server import build_server
from valkeep.transport import public_stdio
from valkeep.upstream import connect_upstream

logging.disable(logging.CRITICAL)


def connector(policy):
    command = (sys.executable, "-I", str(Path(__file__).with_name("reader_stub.py")))
    return connect_upstream(
        policy,
        command=command,
        environment={"FIXTURE_LENGTH": str(policy.target.port)},
    )


async def run():
    config = load_config(Path(sys.argv[1]))
    async with open_router(config, connector=connector) as router:
        server = build_server(router)
        async with public_stdio(config.max_request_bytes) as streams:
            await server.run(*streams, server.create_initialization_options())


asyncio.run(run())
