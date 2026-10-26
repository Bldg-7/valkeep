# Valkeep

운영 Redis를 AI 클라이언트에서 조사할 때, **읽기 전용 권한과 조회 정책을 강제하는 MCP 프록시**.

> 상태: 다중 target stdio 프록시 구현. `list_targets`와 조회 도구 `inspect_key`·`list_length`를 제공하며, 실제 Redis 연결·ACL·운영 부하 검증은 아직 수행하지 않았다. 구체적인 대상 환경과 운영 정책 수치는 미정이다.

## 프로젝트 방향

```text
AI 클라이언트
  → Valkeep: target_id 선택 및 대상별 정책 검증
    ├─ catalog-cache → 전용 공식 Redis MCP → 전용 ACL 계정 → Redis A
    └─ job-queue     → 전용 공식 Redis MCP → 전용 ACL 계정 → Redis B
```

공식 Redis MCP의 조회 기능을 재사용하되, 클라이언트에는 검토한 기능만 제공한다. 기본 정책은 **명시적으로 허용하지 않은 요청은 거절**이다.

- **데이터 변경 방지**: Redis ACL이 보안 경계이며, 도구 필터링은 추가 방어다.
- **조회 부하 완화**: Valkeep이 대상별 호출 빈도, 동시 실행 수, 인자를 제한한다. 키 탐색 기능은 아직 제공하지 않는다.
- **정보 노출 최소화**: 대상·키 범위를 제한하고 값·민감정보를 기본적으로 기록하지 않는다.
- **명시적인 한계**: 읽기도 Redis 자원을 소비한다. 운영 무영향, 정확한 Redis CPU 예산, 실행 중인 명령의 강제 취소는 보장하지 않는다.

## 문서

| 문서 | 내용 |
|---|---|
| [요구사항](docs/requirements.md) | 목적, 범위, 기능·비기능 요구사항, 수용 기준, 미결정 사항 |
| [설계](docs/design.md) | 구성 요소, MCP 처리 흐름, 정책 모델, 조회 안전성, 장애 처리, 검증·도입 계획 |
| [구현·실행 안내](docs/implementation.md) | 현재 구현 범위, 설치·테스트·실행, ACL 준비 사항, 미구현·미검증 항목 |

## 결정 상태

### 대화에서 선택한 방향

- Redis에 읽기 전용으로 접근한다.
- 운영 조회 부하를 통제한다.
- 공식 Redis MCP를 사용하고, 외부 정책 검증 계층을 둔다.

### 초기 구현에서 선택한 사항

- Python 3.12+ 및 MCP SDK 1.30.0, 공식 Redis MCP 0.5.1을 사용한다.
- 하나의 로컬 `stdio` 프록시에 최대 16개의 target을 등록한다. 모든 조회에서 `target_id`를 필수로 받으며 기본 대상은 없다.
- target별 공식 MCP 자식 프로세스·자격증명·키 범위·호출 예산·감사 로그를 분리하고, 업스트림은 직접 노출하지 않는다.
- 위험한 전체 조회 도구를 차단하고, 안전성을 검토한 도구만 제공한다.
- 값 미리보기처럼 공식 도구로 안전하게 구현하기 어려운 기능은 초기 범위에서 제외하거나 별도 어댑터로 추가한다.

### 아직 미정

- 개발/운영 환경, Redis/Valkey 및 버전, standalone/Cluster, managed service 여부.
- 대상 데이터의 크기 특성, 값 조회 필요 여부, 허용 키 범위.
- 실제 운영 배포 형태, 정책 제한값, 감사 로그 보관 정책.

## target 사용 예

설정은 `policy_version = 2`와 `[[targets]]` 목록을 사용한다. [실행 가능한 설정 예시](config/policy.example.toml)와 [이전 설정 마이그레이션](docs/implementation.md)을 참고한다.

```text
list_targets({})
inspect_key({"target_id": "catalog-cache", "key": "example:catalog:42"})
list_length({"target_id": "job-queue", "key": "example:jobs:pending"})
```

`target_id` 생략·미등록 ID는 거절한다. 한 target에서 실패해도 다른 target으로 자동 재시도하지 않는다.

## 참고

- [공식 Redis MCP](https://github.com/redis/mcp-redis)
- [Redis ACL](https://redis.io/docs/latest/operate/oss_and_stack/management/security/acl/)
- [Redis SCAN](https://redis.io/docs/latest/commands/scan/)

공식 MCP의 `main` 코드를 조사한 결과를 설계의 출발점으로 사용했다. 초기 구현은 PyPI `redis-mcp-server==0.5.1`과 `uv.lock`의 배포 파일 해시를 기준으로 고정하고 두 도구의 스키마·출력 형식을 테스트했다. 실제 Redis 명령·ACL·부하는 별도 검증이 필요하다. Redis 호환 프로토콜을 사용한다는 이유만으로 Valkey 및 모든 managed service와의 호환성을 보장하지 않는다.
