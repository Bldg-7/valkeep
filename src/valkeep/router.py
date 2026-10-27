"""명시적 target 선택과 대상별 실행 정책을 연결한다."""

import json
import re
import uuid

from mcp import types

from valkeep.audit import Audit
from valkeep.config import ServerConfig
from valkeep.contracts import MAPPING
from valkeep.errors import Code
from valkeep.proxy import Proxy, failure, response


class Router:
    def __init__(self, config: ServerConfig, proxies: dict[str, Proxy], audit: Audit):
        self.config = config
        self.proxies = proxies
        self.audit = audit

    def tools(self) -> list[types.Tool]:
        tools = [
            types.Tool(
                name="list_targets",
                description=(
                    "등록된 target의 ID·환경·허용 도구·프록시 상태를 확인합니다. "
                    "Valkey/Redis 연결 검사는 아닙니다."
                ),
                inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
                annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False),
            )
        ]
        for name in MAPPING:
            candidates = [proxy for proxy in self.proxies.values() if name in proxy.policy.tools]
            if not candidates:
                continue
            tool = next(tool for tool in candidates[0].tools() if tool.name == name)
            tool.inputSchema["properties"]["target_id"] = {
                "type": "string",
                "enum": [proxy.policy.target.id for proxy in candidates],
                "description": "조회할 target ID. 기본값이나 자동 대체 대상은 없습니다.",
            }
            if "key" in tool.inputSchema["properties"]:
                tool.inputSchema["properties"]["key"]["maxLength"] = max(
                    proxy.policy.limits.max_key_bytes for proxy in candidates
                )
            tool.inputSchema["required"] = ["target_id", *tool.inputSchema["required"]]
            tools.append(tool)
        return tools

    def _record(self, name: str, code: str) -> bool:
        try:
            self.audit.emit("routing", uuid.uuid4().hex, name, code)
            return True
        except Exception:
            return False

    def _reject(self, name: str, code: Code) -> types.CallToolResult:
        if not self._record(name, code.value):
            return failure(Code.AUDIT_UNAVAILABLE)
        return failure(code)

    async def execute(self, name: str, arguments: dict | None) -> types.CallToolResult:
        if self.audit.failed:
            return failure(Code.AUDIT_UNAVAILABLE)
        if name == "list_targets":
            if arguments not in ({}, None):
                return self._reject(name, Code.INVALID_ARGUMENT)
            targets = []
            for target in self.config.targets:
                proxy = self.proxies[target.id]
                unavailable = proxy.closed or proxy.upstream.failed or proxy.audit.failed
                targets.append(
                    {
                        "id": target.id,
                        "environment": target.environment,
                        "engine": target.engine,
                        "tools": list(target.effective_tools),
                        "discovery_scopes": (
                            list(proxy.scans.scopes)
                            if "find_keys" in target.effective_tools
                            else []
                        ),
                        "status": "unavailable" if unavailable else "contract_verified",
                    }
                )
            result = response({"data": {"targets": targets}})
            if len(result.model_dump_json(by_alias=True).encode()) > self.config.max_response_bytes:
                return self._reject(name, Code.RESPONSE_TOO_LARGE)
            if not self._record(name, "OK"):
                return failure(Code.AUDIT_UNAVAILABLE)
            return result
        if name not in MAPPING:
            return self._reject(name, Code.TOOL_DENIED)
        if type(arguments) is not dict:
            return self._reject(name, Code.INVALID_ARGUMENT)
        target_id = arguments.get("target_id")
        if type(target_id) is not str or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", target_id):
            return self._reject(name, Code.INVALID_ARGUMENT)
        proxy = self.proxies.get(target_id)
        if proxy is None:
            return self._reject(name, Code.TARGET_UNKNOWN)
        # 선택한 대상 외의 정책으로 재시도하거나 target_id를 업스트림에 전달하지 않는다.
        forwarded = {key: value for key, value in arguments.items() if key != "target_id"}
        if (
            len(json.dumps(arguments, ensure_ascii=True).encode())
            > proxy.policy.limits.max_request_bytes
        ):
            return self._reject(name, Code.INVALID_ARGUMENT)
        if not self._record(name, "ROUTED"):
            return failure(Code.AUDIT_UNAVAILABLE)
        return await proxy.execute(name, forwarded)
