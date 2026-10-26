"""클라이언트가 Redis cursor를 직접 조작하지 못하게 하는 유한 탐색 상태."""

import secrets
import time
from dataclasses import dataclass, field

from valkeep.config import Policy
from valkeep.errors import Code, Rejected


@dataclass
class ScanState:
    id: str
    scope: str
    prefix: str
    deadline: float
    cursor: int = 0
    calls: int = 0
    observed: int = 0
    seen: set[str] = field(default_factory=set)
    busy: bool = False


class Scans:
    def __init__(self, policy: Policy, clock=time.monotonic):
        self.policy = policy
        self.clock = clock
        self.states: dict[str, ScanState] = {}
        self.tokens: dict[str, str] = {}

    @property
    def scopes(self) -> dict[str, str]:
        return {
            f"prefix-{index}": value for index, value in enumerate(self.policy.allowed_prefixes)
        }

    def abort(self, state: ScanState) -> None:
        self.states.pop(state.id, None)
        self.tokens = {token: id_ for token, id_ in self.tokens.items() if id_ != state.id}

    def validate(self, scope: str, token: str | None) -> None:
        for state in list(self.states.values()):
            if not state.busy and self.clock() >= state.deadline:
                self.abort(state)
        if scope not in self.scopes:
            raise Rejected(Code.KEY_SCOPE_DENIED)
        if token is not None:
            state = self.states.get(self.tokens.get(token, ""))
            if state is None or state.scope != scope or state.busy:
                raise Rejected(Code.CURSOR_INVALID)

    def begin(self, scope: str, token: str | None) -> ScanState:
        self.validate(scope, token)
        if token is None:
            if len(self.states) >= self.policy.limits.scan_sessions:
                raise Rejected(Code.SCAN_BUDGET_EXCEEDED)
            state = ScanState(
                secrets.token_urlsafe(24),
                scope,
                self.scopes[scope],
                self.clock() + self.policy.limits.scan_ttl_seconds,
            )
            self.states[state.id] = state
        else:
            state = self.states[self.tokens.pop(token)]
        if state.calls >= self.policy.limits.scan_calls:
            self.abort(state)
            raise Rejected(Code.SCAN_BUDGET_EXCEEDED)
        state.busy = True
        state.calls += 1
        return state

    def finish(self, state: ScanState, page: dict) -> dict:
        limit = self.policy.limits
        state.cursor = page["cursor"]
        state.observed += page["observed"]
        keys = []
        truncated = page["truncated"]
        for key in page["keys"]:
            if (
                not key.startswith(state.prefix)
                or len(key.encode("utf-8")) > limit.max_key_bytes
                or len(state.seen) >= limit.scan_observations
            ):
                truncated = True
                continue
            if key not in state.seen:
                state.seen.add(key)
                keys.append(key)
        reason = "page_returned"
        if truncated:
            reason = "page_truncated"
        elif state.cursor == 0:
            reason = "scan_complete"
        elif state.calls >= limit.scan_calls:
            reason = "call_budget"
        elif state.observed >= limit.scan_observations:
            reason = "observation_budget"
        elif self.clock() >= state.deadline:
            reason = "time_budget"
        token = None
        if reason == "page_returned":
            state.busy = False
            token = secrets.token_urlsafe(24)
            self.tokens[token] = state.id
        else:
            self.abort(state)
        return {
            "keys": keys,
            "partial": reason != "scan_complete",
            "truncated": truncated,
            "scan_complete": state.cursor == 0,
            "stop_reason": reason,
            "next_token": token,
            "calls": state.calls,
            "observed": state.observed,
            "atomic": False,
        }
