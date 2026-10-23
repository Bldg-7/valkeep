"""고정한 공식 MCP 0.5.1의 두 도구만 허용하는 계약."""

import json
from typing import Any

from mcp import types

from valkeep.errors import Code, Rejected

MAPPING = {"inspect_key": ("type", "key"), "list_length": ("llen", "name")}
REDIS_TYPES = {"none", "string", "list", "set", "zset", "hash", "stream", "vectorset"}


def _semantic_schema(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _semantic_schema(item)
            for key, item in value.items()
            if key not in {"title", "description"}
        }
    if isinstance(value, list):
        return [_semantic_schema(item) for item in value]
    return value


def verify_contracts(tools: list[types.Tool], enabled: tuple[str, ...]) -> int:
    names = [tool.name for tool in tools]
    if len(names) != len(set(names)):
        raise Rejected(Code.UPSTREAM_CONTRACT_MISMATCH)
    indexed = {tool.name: tool for tool in tools}
    for public in enabled:
        upstream, argument = MAPPING[public]
        expected = {
            "type": "object",
            "properties": {argument: {"type": "string"}},
            "required": [argument],
        }
        if upstream not in indexed or _semantic_schema(indexed[upstream].inputSchema) != expected:
            raise Rejected(Code.UPSTREAM_CONTRACT_MISMATCH)
    return len(tools) - len(enabled)


def decode_result(name: str, key: str, result: types.CallToolResult) -> dict:
    if result.isError:
        raise Rejected(Code.UPSTREAM_UNAVAILABLE)
    if len(result.content) != 1 or not isinstance(result.content[0], types.TextContent):
        raise Rejected(Code.UPSTREAM_CONTRACT_MISMATCH)
    try:
        text_value = json.loads(result.content[0].text)
    except (ValueError, RecursionError):
        raise Rejected(Code.UPSTREAM_UNAVAILABLE) from None

    # 고정 릴리스는 두 도구 모두 structuredContent.result로 감싼다.
    structured = result.structuredContent
    expected = {"result": text_value}
    if structured is None or json.dumps(structured, sort_keys=True) != json.dumps(
        expected, sort_keys=True
    ):
        raise Rejected(Code.UPSTREAM_CONTRACT_MISMATCH)

    if name == "inspect_key":
        if (
            not isinstance(text_value, dict)
            or set(text_value) != {"key", "type", "ttl"}
            or text_value["key"] != key
            or type(text_value["type"]) is not str
            or text_value["type"] not in REDIS_TYPES
            or type(text_value["ttl"]) is not int
            or not -2 <= text_value["ttl"] <= 2**63 - 1
        ):
            raise Rejected(Code.UPSTREAM_CONTRACT_MISMATCH)
        # 원본 키를 결과에 반복하지 않는다. TYPE과 TTL은 비원자적 관측이다.
        return {"type": text_value["type"], "ttl_seconds": text_value["ttl"]}
    if type(text_value) is not int or not 0 <= text_value <= 2**63 - 1:
        raise Rejected(Code.UPSTREAM_CONTRACT_MISMATCH)
    return {"length": text_value}
