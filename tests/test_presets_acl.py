"""초보자용 프리셋, 권한안 생성 및 초과 ACL 거절 검증."""

import json

import pytest
from pydantic import ValidationError

from valkeep.acl import permission_rules, plan, verify_export
from valkeep.config import TargetEntry
from valkeep.errors import Rejected
from valkeep.reader import Reader, escape_glob


def entry_from(policy):
    values = policy.model_dump(exclude={"policy_version", "target", "tools"})
    return {**values, **policy.target.model_dump(), "limits": {}}


def test_safe_preset_excludes_values_and_discovery(policy):
    entry = TargetEntry.model_validate(entry_from(policy))
    assert entry.effective_tools == ("inspect_key", "list_length", "connection_status")
    rules = permission_rules(entry.as_policy())
    assert "+getrange" not in rules
    assert "+scan" not in rules
    assert "+get" not in rules
    assert not any(rule.startswith("+@") for rule in rules)


def test_features_are_explicit_and_independent(policy):
    data = entry_from(policy)
    data["features"] = {"value_preview": True}
    target = TargetEntry.model_validate(data)
    assert "preview_key" in target.effective_tools
    assert "find_keys" not in target.effective_tools
    data["features"] = {"key_discovery": True}
    target = TargetEntry.model_validate(data)
    assert "find_keys" in target.effective_tools
    assert "preview_key" not in target.effective_tools


def test_preset_rejects_unbounded_limits_and_ambiguous_tools(policy):
    data = entry_from(policy)
    data["limits"] = {"requests_per_second": 100.0}
    with pytest.raises(ValidationError):
        TargetEntry.model_validate(data)
    data["limits"] = {}
    data["tools"] = ["inspect_key"]
    data["features"] = {"value_preview": True}
    with pytest.raises(ValidationError):
        TargetEntry.model_validate(data)


def test_explicit_safe_preset_cannot_be_bypassed_with_tools(policy):
    data = entry_from(policy)
    data.update(preset="production-safe", tools=["preview_key"])
    with pytest.raises(ValidationError):
        TargetEntry.model_validate(data)
    data.pop("preset")
    assert TargetEntry.model_validate(data).preset == "custom"


def test_acl_plan_has_no_password_and_is_not_applied(policy):
    document = plan(policy)
    assert document["applied"] is False
    assert document["password_configuration_required"]
    assert not any(rule.startswith(">") for rule in document["acl_setuser_arguments"])
    assert "%R~safe:*" in document["acl_setuser_arguments"]


def export_for(policy):
    rules = permission_rules(policy)
    return {
        "flags": ["on"],
        "passwords": ["synthetic-not-a-real-password-hash"],
        "commands": " ".join(rule for rule in rules if rule.startswith(("+", "-"))),
        "keys": " ".join(rule for rule in rules if rule.startswith("%R~")),
        "channels": "",
        "selectors": [],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"commands": "+@all"},
        {"commands": "-@all +@read"},
        {"keys": "~*"},
        {"channels": "&*"},
        {"selectors": [{"commands": "+@all"}]},
        {"flags": ["on", "nopass"]},
        {"passwords": []},
    ],
)
def test_acl_export_fails_closed(policy, tmp_path, change):
    data = export_for(policy)
    data.update(change)
    path = tmp_path / "acl.json"
    path.write_text(json.dumps(data))
    with pytest.raises(Rejected, match="ACL_MISMATCH"):
        verify_export(policy, path)


def test_acl_export_is_explicitly_offline(policy, tmp_path):
    path = tmp_path / "acl.json"
    path.write_text(json.dumps(export_for(policy)))
    assert verify_export(policy, path)["live_account_verified"] is False


def test_glob_escaping_is_literal():
    assert escape_glob("a[*]?\\") == "a\\[\\*\\]\\?\\\\"


def test_reader_never_reads_entire_string_or_unknown_operation(policy):
    calls = []

    class Client:
        def type(self, key):
            return b"string"

        def strlen(self, key):
            return 10_000_000

        def getrange(self, key, start, end):
            calls.append((key, start, end))
            return b"x" * (end + 1)

    data = policy.model_dump()
    data["tools"] = ["preview_key"]
    reader = Reader(Client(), type(policy).model_validate(data))
    value = reader.execute(json.dumps({"operation": "preview_key", "key": "safe:a", "limit": 32}))
    assert value["data"]["returned_bytes"] == 32
    assert calls == [("safe:a", 0, 31)]
    denied = reader.execute(json.dumps({"operation": "execute", "command": "SET"}))
    assert denied["ok"] is False
    assert len(calls) == 1
