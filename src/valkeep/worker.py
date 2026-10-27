"""target별로 실행되는 최소 MCP 읽기 프로세스. 쓰기 도구를 등록하지 않는다."""

import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from valkeep.config import Policy
from valkeep.reader import Reader, create_client


def build_worker(reader: Reader, policy: Policy | None = None) -> FastMCP:
    mcp = FastMCP("Valkeep Valkey Reader")

    if policy is None or any(tool != "list_length" for tool in policy.tools):

        @mcp.tool()
        async def valkeep_read(request: str) -> dict[str, Any]:
            """검증된 내부 조회 요청을 실행한다."""
            return reader.execute(request)

    if policy is None or "list_length" in policy.tools:

        @mcp.tool()
        async def llen(name: str) -> int:
            """정책에서 허용한 List의 원소 수만 조회한다."""
            reader._check_key(name)
            return reader.client.llen(name)

    return mcp


def main():
    logging.disable(logging.CRITICAL)
    policy = Policy.model_validate_json(os.environ["VALKEEP_POLICY"])
    client = create_client(policy)
    try:
        build_worker(Reader(client, policy), policy).run()
    finally:
        client.close()


if __name__ == "__main__":
    main()
