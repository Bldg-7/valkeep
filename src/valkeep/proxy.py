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
from valkeep.operations import public_tools
from valkeep.scans import Scans, ScanState
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
        self.scans = Scans(policy)

    def tools(self) -> list[types.Tool]:
        return public_tools(self.policy)

    def validate(self, name: str, arguments: dict | None) -> dict:
        if name not in self.policy.tools:
            raise Rejected(Code.TOOL_DENIED)
        if type(arguments) is not dict:
            raise Rejected(Code.INVALID_ARGUMENT)
        if name == "connection_status":
            if arguments:
                raise Rejected(Code.INVALID_ARGUMENT)
            return arguments
        if name == "find_keys":
            if set(arguments) not in ({"scope_id"}, {"scope_id", "token"}):
                raise Rejected(Code.INVALID_ARGUMENT)
            scope, token = arguments["scope_id"], arguments.get("token")
            if type(scope) is not str or (
                "token" in arguments and (type(token) is not str or not 1 <= len(token) <= 128)
            ):
                raise Rejected(Code.INVALID_ARGUMENT)
            self.scans.validate(scope, token)
            return arguments
        if set(arguments) != {"key"}:
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
        return arguments

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
            arguments = self.validate(name, arguments)
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

        state = None
        try:
            if name == "find_keys":
                state = self.scans.begin(arguments["scope_id"], arguments.get("token"))
        except Rejected as exc:
            self.limiter.release()
            if not self._emit("rejected", request_id, name, exc.code.value):
                return failure(Code.AUDIT_UNAVAILABLE)
            return failure(exc.code)
        worker = asyncio.create_task(self._run(request_id, name, arguments, state))
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
            # 응답 대기 취소와 실제 서버 호출의 수명을 분리한다.
            self._emit("response_cancelled", request_id, name, "CANCELLED")
            raise

    async def _run(
        self, request_id: str, name: str, arguments: dict, state: ScanState | None
    ) -> types.CallToolResult:
        started = time.monotonic()
        upstream_name, argument = MAPPING[name]
        if name == "list_length":
            forwarded = {argument: arguments["key"]}
        else:
            request = {"operation": name, **arguments}
            if name == "preview_key":
                request["limit"] = self.policy.limits.preview_bytes
            if state is not None:
                request = {
                    "operation": name,
                    "prefix": state.prefix,
                    "cursor": state.cursor,
                    "count": min(
                        self.policy.limits.scan_count_hint,
                        max(1, self.policy.limits.scan_observations - state.observed),
                    ),
                }
            forwarded = {argument: json.dumps(request, ensure_ascii=True)}
        call = asyncio.create_task(self.upstream.call(upstream_name, forwarded))
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
            data = decode_result(name, arguments.get("key", ""), raw)
            if name == "preview_key" and data["returned_bytes"] > self.policy.limits.preview_bytes:
                raise Rejected(Code.UPSTREAM_CONTRACT_MISMATCH)
            if state is not None:
                data = self.scans.finish(state, data)
            result = response(
                {
                    "data": data,
                    "meta": {
                        "target_id": self.policy.target.id,
                        "partial": data.get("partial", data.get("truncated", False)),
                        "truncated": data.get("truncated", False),
                        "upstream_calls": 1,
                    },
                }
            )
            if state is not None and len(result.model_dump_json(by_alias=True).encode()) > (
                self.policy.limits.max_response_bytes
            ):
                self.scans.abort(state)
                data.update(
                    truncated=True, partial=True, next_token=None, stop_reason="response_size"
                )
                while True:
                    result = response(
                        {
                            "data": data,
                            "meta": {
                                "target_id": self.policy.target.id,
                                "partial": True,
                                "truncated": True,
                                "upstream_calls": 1,
                            },
                        }
                    )
                    if (
                        len(result.model_dump_json(by_alias=True).encode())
                        <= (self.policy.limits.max_response_bytes)
                        or not data["keys"]
                    ):
                        break
                    data["keys"].pop()
            if len(result.model_dump_json(by_alias=True).encode()) > (
                self.policy.limits.max_response_bytes
            ):
                raise Rejected(Code.RESPONSE_TOO_LARGE)
            code = "OK"
        except Rejected as exc:
            if state is not None:
                self.scans.abort(state)
            result = failure(exc.code)
            code = exc.code.value
        except Exception:
            if state is not None:
                self.scans.abort(state)
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
