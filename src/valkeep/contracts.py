"""공식 LLEN과 Valkeep 내부 읽기 어댑터의 고정 계약."""

import base64
import json
from typing import Annotated, Any, Literal

from mcp import types
from pydantic import Field, ValidationError, model_validator

from valkeep.config import FrozenModel
from valkeep.errors import Code, Rejected

MAPPING = {
    "inspect_key": ("valkeep_read", "request"),
    "list_length": ("llen", "name"),
    "preview_key": ("valkeep_read", "request"),
    "find_keys": ("valkeep_read", "request"),
    "connection_status": ("valkeep_read", "request"),
}
Size = Annotated[int, Field(ge=0, le=2**63 - 1)]


class Inspection(FrozenModel):
    type: Annotated[str, Field(min_length=1, max_length=64)]
    ttl_ms: Annotated[int, Field(ge=-2, le=2**63 - 1)]
    length: Size | None
    length_unit: Literal["bytes", "items", "fields", "entries"] | None
    atomic: Literal[False]


class Preview(FrozenModel):
    value: Annotated[str, Field(max_length=8192)]
    encoding: Literal["utf-8", "base64"]
    returned_bytes: Annotated[int, Field(ge=0, le=4096)]
    total_bytes_observed: Size
    truncated: bool
    atomic: Literal[False]

    @model_validator(mode="after")
    def check_bytes(self):
        raw = (
            self.value.encode("utf-8")
            if self.encoding == "utf-8"
            else base64.b64decode(self.value, validate=True)
        )
        if len(raw) != self.returned_bytes:
            raise ValueError("미리보기 길이가 일치하지 않습니다")
        return self


class ScanPage(FrozenModel):
    cursor: Annotated[int, Field(ge=0, le=2**64 - 1)]
    keys: Annotated[list[str], Field(max_length=100)]
    observed: Size
    truncated: bool

    @model_validator(mode="after")
    def check_count(self):
        if self.observed < len(self.keys):
            raise ValueError("관측 키 수가 일치하지 않습니다")
        return self


class Connection(FrozenModel):
    connected: Literal[True]


OUTPUTS = {
    "inspect_key": Inspection,
    "preview_key": Preview,
    "find_keys": ScanPage,
    "connection_status": Connection,
}


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
    required = {MAPPING[public] for public in enabled}
    for upstream, argument in required:
        expected = {
            "type": "object",
            "properties": {argument: {"type": "string"}},
            "required": [argument],
        }
        if upstream not in indexed or _semantic_schema(indexed[upstream].inputSchema) != expected:
            raise Rejected(Code.UPSTREAM_CONTRACT_MISMATCH)
    return len(tools) - len(required)


def decode_result(name: str, key: str, result: types.CallToolResult) -> dict:
    if result.isError:
        raise Rejected(Code.UPSTREAM_UNAVAILABLE)
    if len(result.content) != 1 or not isinstance(result.content[0], types.TextContent):
        raise Rejected(Code.UPSTREAM_CONTRACT_MISMATCH)
    try:
        value = json.loads(result.content[0].text)
        if result.structuredContent is None or json.dumps(
            result.structuredContent, sort_keys=True
        ) != json.dumps({"result": value} if name == "list_length" else value, sort_keys=True):
            raise Rejected(Code.UPSTREAM_CONTRACT_MISMATCH)
        if name == "list_length":
            if type(value) is not int or not 0 <= value <= 2**63 - 1:
                raise Rejected(Code.UPSTREAM_CONTRACT_MISMATCH)
            return {"length": value}
        if type(value) is not dict:
            raise Rejected(Code.UPSTREAM_CONTRACT_MISMATCH)
        if value.get("ok") is False and set(value) == {"ok", "code"}:
            raise Rejected(Code(value["code"]))
        if value.get("ok") is not True or set(value) != {"ok", "data"}:
            raise Rejected(Code.UPSTREAM_CONTRACT_MISMATCH)
        return OUTPUTS[name].model_validate(value["data"]).model_dump()
    except (ValueError, TypeError, UnicodeError, RecursionError, ValidationError):
        raise Rejected(Code.UPSTREAM_CONTRACT_MISMATCH) from None
