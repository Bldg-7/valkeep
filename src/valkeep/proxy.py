"""정책 검사, 실행 제한, 지연 호출 격리를 담당한다."""

import asyncio
import json
import time
import uuid

from mcp import types

from valkeep.audit import Audit
from valkeep.config import Policy
from valkeep.contracts import MAPPING, decode_result
from valkeep.errors import Code, Rejected
from valkeep.limiter import Limiter
from valkeep.upstream import Upstream


def response(payload: dict, *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=True))],
        structuredContent=payload,
        isError=error,
    )


def failure(code: Code) -> types.CallToolResult:
    return response({"error": {"code": code.value}}, error=True)


class Proxy:
    def __init__(self, policy: Policy, upstream: Upstream, audit: Audit):
        self.policy = policy
        self.upstream = upstream
        self.audit = audit
        self.limiter = Limiter(policy.limits)
        self.tasks: set[asyncio.Task] = set()
        self.closed = False

    def tools(self) -> list[types.Tool]:
        descriptions = {
            "inspect_key": "허용된 키의 타입과 TTL만 조회합니다. 두 값은 원자적 관측이 아닙니다.",
            "list_length": "허용된 List의 길이만 조회합니다. 값이나 원소는 읽지 않습니다.",
        }
        return [
            types.Tool(
                name=name,
                description=descriptions[name],
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": self.policy.limits.max_key_bytes,
                        }
                    },
                    "required": ["key"],
                    "additionalProperties": False,
                },
                annotations=types.ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            )
            for name in self.policy.tools
        ]

    def validate(self, name: str, arguments: dict | None) -> str:
        if name not in self.policy.tools:
            raise Rejected(Code.TOOL_DENIED)
        if type(arguments) is not dict or set(arguments) != {"key"}:
            raise Rejected(Code.INVALID_ARGUMENT)
        key = arguments["key"]
        if type(key) is not str or not key:
            raise Rejected(Code.INVALID_ARGUMENT)
        try:
            if len(key.encode("utf-8")) > self.policy.limits.max_key_bytes:
                raise Rejected(Code.INVALID_ARGUMENT)
        except UnicodeError:
            raise Rejected(Code.INVALID_ARGUMENT) from None
        if key not in self.policy.allowed_keys and not any(
            key.startswith(prefix) for prefix in self.policy.allowed_prefixes
        ):
            raise Rejected(Code.KEY_SCOPE_DENIED)
        return key

    def _emit(self, *args, **kwargs) -> bool:
        try:
            self.audit.emit(*args, **kwargs)
            return True
        except Rejected:
            self.closed = True
            return False

    async def execute(self, name: str, arguments: dict | None) -> types.CallToolResult:
        request_id = uuid.uuid4().hex
        try:
            key = self.validate(name, arguments)
            if self.audit.failed:
                raise Rejected(Code.AUDIT_UNAVAILABLE)
            if self.closed or self.upstream.failed:
                raise Rejected(Code.UPSTREAM_UNAVAILABLE)
            self.limiter.acquire()
        except Rejected as exc:
            if not self._emit("rejected", request_id, name, exc.code.value):
                return failure(Code.AUDIT_UNAVAILABLE)
            return failure(exc.code)

        if not self._emit("started", request_id, name, "ACCEPTED"):
            self.limiter.release()
            return failure(Code.AUDIT_UNAVAILABLE)

        worker = asyncio.create_task(self._run(request_id, name, key))
        self.tasks.add(worker)
        worker.add_done_callback(self.tasks.discard)
        try:
            async with asyncio.timeout(self.policy.limits.response_timeout_seconds):
                return await asyncio.shield(worker)
        except TimeoutError:
            if not self._emit("response_timeout", request_id, name, Code.TIMEOUT.value):
                return failure(Code.AUDIT_UNAVAILABLE)
            return failure(Code.TIMEOUT)
        except asyncio.CancelledError:
            # 응답 대기 취소와 실제 Redis 호출의 수명을 분리한다.
            self._emit("response_cancelled", request_id, name, "CANCELLED")
            raise

    async def _run(self, request_id: str, name: str, key: str) -> types.CallToolResult:
        started = time.monotonic()
        upstream_name, argument = MAPPING[name]
        call = asyncio.create_task(self.upstream.call(upstream_name, {argument: key}))
        result = failure(Code.UPSTREAM_UNAVAILABLE)
        code = Code.UPSTREAM_UNAVAILABLE.value
        try:
            hard_timeout = (
                self.policy.limits.response_timeout_seconds
                + self.policy.limits.cleanup_timeout_seconds
            )
            done, _ = await asyncio.wait({call}, timeout=hard_timeout)
            if not done:
                self.closed = True
                await self.upstream.stop()
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)
                raise Rejected(Code.TIMEOUT)
            raw = call.result()
            data = decode_result(name, key, raw)
            result = response(
                {"data": data, "meta": {"partial": False, "truncated": False, "upstream_calls": 1}}
            )
            if len(result.model_dump_json(by_alias=True).encode()) > (
                self.policy.limits.max_response_bytes
            ):
                raise Rejected(Code.RESPONSE_TOO_LARGE)
            code = "OK"
        except Rejected as exc:
            result = failure(exc.code)
            code = exc.code.value
        except Exception:
            result = failure(Code.UPSTREAM_UNAVAILABLE)
        finally:
            if not call.done():
                # 정상 종료 이외 경로에서도 슬롯을 반환하기 전에 자식을 정리한다.
                self.closed = True
                await self.upstream.stop()
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)
            self.limiter.release()
        if not self._emit(
            "finished",
            request_id,
            name,
            code,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            response_bytes=len(result.model_dump_json(by_alias=True).encode()),
        ):
            return failure(Code.AUDIT_UNAVAILABLE)
        return result

    async def close(self) -> None:
        self.closed = True
        await self.upstream.stop()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
