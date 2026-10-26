"""권한을 변경하지 않는 ACL 계획 생성과 관리자 export의 보수적 검증."""

import json
from pathlib import Path

from valkeep.config import Policy
from valkeep.errors import Code, Rejected
from valkeep.reader import escape_glob

COMMANDS = {
    "inspect_key": {"type", "pttl", "strlen", "llen", "hlen", "scard", "zcard", "xlen"},
    "list_length": {"llen"},
    "preview_key": {"type", "strlen", "getrange"},
    "find_keys": {"scan"},
    "connection_status": {"ping"},
}


def permission_rules(policy: Policy) -> list[str]:
    commands = {"client|setinfo"}
    for tool in policy.tools:
        commands.update(COMMANDS[tool])
    if policy.target.database:
        commands.add("select")
    keys = ["%R~" + escape_glob(key) for key in policy.allowed_keys] + [
        "%R~" + escape_glob(prefix) + "*" for prefix in policy.allowed_prefixes
    ]
    if any(any(char.isspace() or ord(char) < 32 for char in key) for key in keys):
        # ACL GETUSER 문자열의 공백 구분을 모호하게 만드는 범위는 자동 검증하지 않는다.
        raise Rejected(Code.CONFIG_INVALID)
    return ["-@all", *sorted(set(keys)), *["+" + name for name in sorted(commands)]]


def plan(policy: Policy) -> dict:
    return {
        "target_id": policy.target.id,
        "redis_minimum_version": "7.2",
        "acl_setuser_arguments": [
            "ACL",
            "SETUSER",
            policy.target.username,
            "reset",
            "on",
            *permission_rules(policy),
        ],
        "password_configuration_required": True,
        "applied": False,
        "warnings": [
            "관리자가 검토한 뒤 별도로 적용해야 합니다. reset은 기존 비밀번호·권한을 제거합니다.",
            "비밀번호는 포함하지 않았습니다. 별도 보안 경로로 설정해야 인증할 수 있습니다.",
            "SCAN을 허용하면 ACL 키 패턴만으로 키 이름 열거를 격리할 수 없습니다.",
        ],
    }


def verify_export(policy: Policy, path: Path) -> dict:
    try:
        if path.stat().st_size > 65536:
            raise ValueError
        data = json.loads(path.read_text())
        # redis-cli --json ACL GETUSER의 RESP2 결과도 지원한다.
        if isinstance(data, list):
            if len(data) % 2:
                raise ValueError
            if len(set(data[::2])) != len(data) // 2:
                raise ValueError
            data = dict(zip(data[::2], data[1::2], strict=True))
        if not isinstance(data, dict):
            raise ValueError
        flags = data.get("flags")
        if not isinstance(flags, list) or not set(flags) <= {"on", "sanitize-payload"}:
            raise ValueError
        if (
            "on" not in flags
            or not isinstance(data.get("passwords"), list)
            or not data["passwords"]
        ):
            raise ValueError
        if data.get("selectors") != [] or data.get("channels") != "":
            raise ValueError
        rules = permission_rules(policy)
        expected_commands = {"-@all", *(item for item in rules if item.startswith("+"))}
        actual_commands = data["commands"].split()
        if (
            not actual_commands
            or actual_commands[0] != "-@all"
            or set(actual_commands) != expected_commands
            or len(actual_commands) != len(expected_commands)
        ):
            raise ValueError
        expected_keys = {item for item in rules if item.startswith("%R~")}
        actual_keys = data["keys"].split()
        if set(actual_keys) != expected_keys or len(actual_keys) != len(expected_keys):
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise Rejected(Code.ACL_MISMATCH) from None
    return {
        "target_id": policy.target.id,
        "matches_policy": True,
        "verification": "offline_acl_export",
        "live_account_verified": False,
    }
