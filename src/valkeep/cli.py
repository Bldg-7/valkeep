"""운영 연결을 자동 추정하지 않는 stdio 실행 진입점."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from valkeep.config import load_config
from valkeep.errors import Code, Rejected
from valkeep.runtime import open_router
from valkeep.server import build_server
from valkeep.transport import public_stdio


async def serve(path: Path) -> None:
    config = load_config(path)
    async with open_router(config) as router:
        server = build_server(router)
        async with public_stdio(config.max_request_bytes) as streams:
            await server.run(*streams, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(description="읽기 전용 Redis MCP 정책 프록시")
    parser.add_argument("--config", required=True, type=Path, help="TOML 정책 파일")
    parser.add_argument("--check-config", action="store_true", help="연결 없이 설정만 검증")
    args = parser.parse_args()
    # SDK 내부 예외가 원본 인자·업스트림 메시지를 stderr로 출력하지 않게 한다.
    logging.disable(logging.CRITICAL)
    try:
        if args.check_config:
            load_config(args.config)
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
