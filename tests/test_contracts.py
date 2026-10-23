"""스키마 및 구조화 응답의 모호한 해석을 거절한다."""

import json

import pytest
from mcp import types
from pydantic import ValidationError

from valkeep.config import Policy
from valkeep.contracts import decode_result
from valkeep.errors import Rejected


def tool_result(value, structured):
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(value))],
        structuredContent=structured,
    )


@pytest.mark.parametrize(
    "value,structured",
    [
        (1, None),
        (1, {"result": True}),
        (1, {"result": 2}),
        (-1, {"result": -1}),
        (True, {"result": True}),
        (1.0, {"result": 1.0}),
        (2**64, {"result": 2**64}),
    ],
)
def test_invalid_length_contract(value, structured):
    with pytest.raises(Rejected):
        decode_result("list_length", "safe:a", tool_result(value, structured))


@pytest.mark.parametrize(
    "changes",
    [
        {"key": "other:key"},
        {"type": ["list"]},
        {"type": "unknown-module"},
        {"ttl": True},
        {"ttl": -3},
        {"ttl": 2**64},
        {"unexpected": "secret"},
    ],
)
def test_invalid_metadata_contract(changes):
    value = {"key": "safe:a", "type": "list", "ttl": -1}
    value.update(changes)
    with pytest.raises(Rejected):
        decode_result("inspect_key", "safe:a", tool_result(value, {"result": value}))


@pytest.mark.parametrize("confirmation", [False, 1, "true", None])
def test_acl_requires_explicit_boolean(policy, confirmation):
    data = policy.model_dump()
    data["target"]["readonly_acl_confirmed"] = confirmation
    with pytest.raises(ValidationError):
        Policy.model_validate(data)
