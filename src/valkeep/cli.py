"""운영 연결을 자동 추정하지 않는 stdio 실행 진입점."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from valkeep.audit import Audit
from valkeep.config import load_policy
from valkeep.errors import Code, Rejected
from valkeep.proxy import Proxy
from valkeep.server import build_server
from valkeep.transport import public_stdio
from valkeep.upstream import connect_upstream


async def serve(path: Path) -> None:
    policy = load_policy(path)
    audit = Audit(policy)
    try:
        async with connect_upstream(policy) as upstream:
            proxy = Proxy(policy, upstream, audit)
            audit.emit("startup", "-", "-", "CONTRACT_VERIFIED", ignored_tools=upstream.extra_tools)
            server = build_server(proxy)
            try:
                async with public_stdio(policy.limits.max_request_bytes) as streams:
                    await server.run(*streams, server.create_initialization_options())
            finally:
                await proxy.close()
    finally:
        audit.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="읽기 전용 Redis MCP 정책 프록시")
    parser.add_argument("--config", required=True, type=Path, help="TOML 정책 파일")
    parser.add_argument("--check-config", action="store_true", help="연결 없이 설정만 검증")
    args = parser.parse_args()
    # SDK 내부 예외가 원본 인자·업스트림 메시지를 stderr로 출력하지 않게 한다.
    logging.disable(logging.CRITICAL)
    try:
        if args.check_config:
            load_policy(args.config)
            print("설정 검증 완료 (Redis 연결·ACL 검증은 수행하지 않음)", file=sys.stderr)
        else:
            asyncio.run(serve(args.config))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        code = exc.code if isinstance(exc, Rejected) else Code.UPSTREAM_UNAVAILABLE
        print(f"valkeep: {code.value}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
