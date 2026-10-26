"""운영 연결을 자동 추정하지 않는 stdio 실행 진입점."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from valkeep.acl import plan, verify_export
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


async def check_connections(config) -> dict:
    async with open_router(config) as router:
        results = {}
        for target in config.targets:
            result = await router.execute("connection_status", {"target_id": target.id})
            results[target.id] = result.structuredContent
        return results


def main() -> None:
    parser = argparse.ArgumentParser(description="읽기 전용 Redis MCP 정책 프록시")
    parser.add_argument("--config", required=True, type=Path, help="TOML 정책 파일")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-config", action="store_true", help="연결 없이 설정만 검증")
    mode.add_argument("--acl-plan", action="store_true", help="적용하지 않고 ACL 권한안 출력")
    mode.add_argument("--verify-acl", type=Path, help="관리자가 제공한 ACL GETUSER JSON 검증")
    mode.add_argument("--check-connections", action="store_true", help="명시한 대상에 PING 실행")
    parser.add_argument("--target", help="진단·ACL 작업의 등록 target ID")
    args = parser.parse_args()
    if args.verify_acl and not args.target:
        parser.error("--verify-acl에는 --target이 필요합니다")
    if args.target and not (args.acl_plan or args.verify_acl or args.check_connections):
        parser.error("--target은 ACL·연결 점검 모드에서만 사용할 수 있습니다")
    # SDK 내부 예외가 원본 인자·업스트림 메시지를 stderr로 출력하지 않게 한다.
    logging.disable(logging.CRITICAL)
    try:
        config = load_config(args.config)
        if args.target:
            selected = tuple(target for target in config.targets if target.id == args.target)
            if not selected:
                raise Rejected(Code.TARGET_UNKNOWN)
            config = config.model_copy(update={"targets": selected})
        if args.check_config:
            print("설정 검증 완료 (Redis 연결·ACL 검증은 수행하지 않음)", file=sys.stderr)
        elif args.acl_plan:
            print(
                json.dumps(
                    [plan(target.as_policy()) for target in config.targets], ensure_ascii=False
                )
            )
        elif args.verify_acl:
            print(json.dumps(verify_export(config.targets[0].as_policy(), args.verify_acl)))
        elif args.check_connections:
            results = asyncio.run(check_connections(config))
            print(json.dumps(results))
            if any("error" in result for result in results.values()):
                raise SystemExit(1)
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
