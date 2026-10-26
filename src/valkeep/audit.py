"""값·키·임의 오류 문자열을 받지 않는 감사 출력."""

import json
import os
import stat
import time

from valkeep.config import Policy, ServerConfig
from valkeep.errors import Code, Rejected


class Audit:
    def __init__(self, policy: Policy | ServerConfig):
        self.policy = policy
        self.failed = False
        self.fd = -1
        try:
            self.fd = os.open(
                policy.audit_path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            info = os.fstat(self.fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise OSError("감사 파일 접근 권한이 올바르지 않습니다")
        except OSError:
            self.close()
            raise Rejected(Code.AUDIT_UNAVAILABLE) from None

    def emit(
        self,
        event: str,
        request_id: str,
        tool: str,
        code: str,
        *,
        elapsed_ms: int = 0,
        response_bytes: int = 0,
        ignored_tools: int = 0,
    ) -> None:
        if self.failed:
            raise Rejected(Code.AUDIT_UNAVAILABLE)
        record = {
            "time": time.time(),
            "target": self.policy.target.id if isinstance(self.policy, Policy) else None,
            "policy": self.policy.fingerprint,
            "event": event,
            "request_id": request_id,
            "tool": tool
            if tool
            in {
                "inspect_key",
                "list_length",
                "list_targets",
                "preview_key",
                "find_keys",
                "connection_status",
            }
            else "unlisted",
            "code": code,
            "elapsed_ms": elapsed_ms,
            "response_bytes": response_bytes,
            "ignored_tools": ignored_tools,
        }
        payload = (json.dumps(record, separators=(",", ":")) + "\n").encode()
        try:
            if os.write(self.fd, payload) != len(payload):
                raise OSError("감사 기록이 완료되지 않았습니다")
        except OSError:
            self.failed = True
            raise Rejected(Code.AUDIT_UNAVAILABLE) from None

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
