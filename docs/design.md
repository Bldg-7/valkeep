# Valkeep 설계

> 아래 내용은 목표 설계다. 현재는 단계 B의 최소 프록시를 구현했으며, 실제 동작·설정 형식과 미구현 범위는 [구현·실행 안내](implementation.md)를 우선 참고한다.

## 1. 설계 요약

**target마다 공식 Redis MCP를 별도 자식 프로세스로 실행하는 다중 대상 정책 프록시**를 사용한다. 클라이언트에는 Valkeep만 등록하고, 공식 MCP는 외부에 노출하지 않는다.

프록시는 단순한 도구 목록 필터가 아니다. 실제 호출에서 대상·도구·인자·조회 예산을 검사하고, 결과와 오류도 정책에 맞게 변환한다.

```text
AI 클라이언트
   │ MCP / stdio
   ▼
Valkeep
   ├─ 공개 도구 계약: 자체 스키마와 설명
   ├─ 정책 엔진: 도구·대상·키·인자 검증
   ├─ 실행 제어: rate limit, 동시 실행, 탐색 예산
   ├─ 결과 변환: 크기 제한, 부분 결과, 오류 정제
   ├─ 감사: 값 없는 구조화 로그
   │
   ├─ target A: 전용 수명 태스크·정책·limiter·감사
   │     └─ 공식 Redis MCP 자식 A → ACL 계정 A → Redis A
   └─ target B: 전용 수명 태스크·정책·limiter·감사
         └─ 공식 Redis MCP 자식 B → ACL 계정 B → Redis B
```

별도 어댑터가 필요한 안전한 값 미리보기는 선택적 후속 범위다. 첫 버전부터 범용 Redis 실행 경로를 추가하지 않는다.

## 2. 주요 설계 결정

| 결정 | 이유 | 제약 |
|---|---|---|
| 기본 거부 도구 허용 목록 | 신규 도구·임의 이름 호출을 차단 | 도구 추가 시 검토 필요 |
| 공개 스키마와 업스트림 스키마 분리 | 공개 API에서 위험한 인자를 제거 | 명시적 매핑·호환성 테스트 필요 |
| 공식 서버 버전 고정 | 업데이트에 따른 정책 우회 방지 | 보안 업데이트를 검증 후 반영 |
| 한 프록시에 여러 target, target당 별도 자식 | 자격증명·제한·수명 격리 | 최대 16개, 모든 조회에 target_id 필수 |
| 제한된 stdio 기능만 제공 | 네트워크 인증·멀티테넌트 복잡성 축소 | 중앙 서비스는 별도 설계 |
| 위험한 조회는 우선 차단 | 사후 응답 절단만으로는 Redis 부하를 줄이지 못함 | 일부 값 조회 기능 감소 |
| Redis ACL과 프록시 정책 병행 | 프록시 오류가 곧 쓰기 허용으로 이어지지 않게 함 | ACL은 빈도·인자·SCAN 탐색량을 제한하지 못함 |

로컬 배포에서는 동일 OS 사용자에게서 자격증명과 프로세스를 완전히 숨길 수 없다. 조직 차원의 강제 통제가 필요하면 중앙 서비스가 자격증명과 네트워크 접근을 독점하는 형태가 필요하다.

## 3. 구성 요소

### 3.1 공개 MCP 서버

- MCP SDK로 초기화·협상·종료·tools 요청을 처리한다.
- 공개 도구 이름·설명·스키마는 Valkeep이 소유한다.
- 검토한 capability만 광고하며 업스트림 capability를 그대로 복제하지 않는다.
- stdio stdout은 MCP 프레임 전용이다. 로그는 별도 출력으로 분리한다.

### 3.2 대상 라우터와 수명 관리자

- `list_targets`와 target별 허용 도구의 합집합을 공개한다. 개별 도구의 `target_id` enum에는 해당 도구가 허용된 대상만 넣는다.
- 실제 호출에서도 ID·도구·키 정책을 재검사한다. schema나 UI 필터만 신뢰하지 않는다.
- 기본 target, 실행 중 연결 설정 변경, 다른 target으로의 자동 재시도는 없다.
- target마다 별도 asyncio 태스크가 MCP ClientSession과 자식 프로세스의 생성·종료를 소유한다. 여러 세션의 취소 범위를 하나의 수명 태스크에 중첩하지 않는다.
- target마다 독립 Proxy·limiter·감사 파일을 사용한다. 라우터 감사 파일은 target 식별 전의 거절과 목록·분기 이벤트를 기록한다.
- 하나의 target만 실패하면 해당 target을 사용 불가로 표시한다. 공통 감사 실패는 모든 새 조회를 차단한다.

### 3.3 정책 엔진

- 불변 설정을 로드하고 정규화한다.
- 도구 허용 목록, 대상, 키 범위, 입력 길이와 범위를 검사한다.
- 검증 성공 시에만 구체적인 업스트림 호출 계획을 생성한다.
- 클라이언트가 주는 URL·파일 경로·계정·임의 Redis 명령을 연결 설정으로 사용하지 않는다.

### 3.4 실행 제어기

- 대상별 token bucket 등으로 rate limit을 적용한다.
- 대상별 실행 슬롯으로 동시 업스트림 호출 수를 제한한다.
- 한 공개 도구가 여러 업스트림 호출을 한다면 각 호출을 예산에 포함한다.
- 업스트림 도구 내부의 Redis 명령 수는 다를 수 있다. 예를 들어 타입 검사 도구가 TYPE과 TTL을 모두 실행하면, MCP 호출 수와 Redis 명령 수는 같지 않다.
- 초기 도구는 내부 호출 수를 검토 가능한 범위로 제한하고 전체 순회 도구를 금지한다.
- 프로세스별 limiter는 여러 인스턴스의 합산 트래픽을 통제하지 못한다. 초기에는 같은 대상을 향한 인스턴스 수를 관리하고, 수평 확장 시 공유 limiter를 설계한다.

### 3.5 업스트림 어댑터

- 공식 Redis MCP를 고정된 실행 파일·버전으로 시작한다.
- 자식 프로세스 환경은 필요한 값만 전달하며 자격증명을 명령행 인자에 넣지 않는다.
- 초기화 후 도구 목록과 필수 스키마를 검증한다.
- 도구별 인자와 응답을 명시적으로 변환한다. 반환 문자열을 임의 코드나 지시로 해석하지 않는다.
- 업스트림이 오류를 일반 문자열로 반환하는 경우도 있으므로 도구별 검증된 응답 계약으로 정상·오류를 구분한다. 알 수 없는 응답은 성공으로 추정하지 않는다.
- 프레임 크기 제한을 적용할 수 있는 SDK/transport를 선택한다. 초과 시 처리 중단과 프로세스 정리를 수행하되, 이미 발생한 Redis 읽기 비용을 되돌릴 수는 없다.

### 3.6 결과 및 감사 처리

- 반환 항목 수·직렬화 후 UTF-8 바이트 수를 제한한다.
- 크기 제한은 프레임을 임의 바이트로 잘라 JSON을 손상시키는 방식이 아니라, 구조화된 결과를 축소하여 적용한다.
- 값·키 이름·오류 원문은 기본 로그에서 제외한다.
- 감사 실패 정책은 배포 전에 확정한다. 엄격한 감사가 필요한 운영 배포는 요청 시작 감사 기록에 실패하면 새 조회를 거절한다.

## 4. 요청 처리 흐름

### 4.1 시작

1. 설정을 검증하고 정책 버전 식별자를 생성한다.
2. 필수 제한값 누락·모순이 있으면 시작을 거절한다.
3. 모든 target의 자격증명 존재를 확인한 후 target별 공식 MCP 자식 프로세스를 시작한다.
4. 각 업스트림의 초기화와 필수 도구 계약 검사를 수행한다. 하나라도 실패하면 시작한 대상을 모두 정리한다.
5. 모두 검증되면 공개 MCP를 시작한다. 실행 중에는 target별 실패를 격리한다.

Redis 연결이 지연 생성되는 업스트림에서는 도구 계약 검사 성공과 Redis 접속 성공을 구분한다. 연결 점검은 명시적으로 허용한 안전한 방법으로 수행하고, 업스트림 초기화만으로 Redis 정상 상태를 보고하지 않는다.

### 4.2 도구 호출

1. 요청 크기와 MCP 메서드를 검증한다.
2. 도구 허용 목록과 공개 입력 스키마를 검사한다.
3. 명시적인 target ID로 대상을 선택하고, 그 대상의 키 범위·범위 인자·탐색 토큰·예산을 검사한다.
4. rate limit·동시 실행 슬롯을 원자적으로 확보한다.
5. 감사 시작 기록 후 검증된 인자만 업스트림에 전달한다.
6. 응답 계약을 검사하고 결과 크기·마스킹·부분 결과 정책을 적용한다.
7. 실제 종료 상태를 기록하고 슬롯을 반환한다.

거절된 호출도 별도 감사 이벤트로 남긴다. 내부 재시도는 기본 비활성화한다. 재시도가 필요하면 횟수·대기 시간·전체 예산을 별도로 정의한다.

### 4.3 timeout과 취소

클라이언트 응답 제한 시간과 실제 업스트림 실행 수명은 별개다.

- 응답 제한 시간이 지나면 클라이언트에는 timeout을 알린다.
- 취소를 전달할 수 있으면 전달하되, 동기 Redis 호출이 즉시 중단된다고 가정하지 않는다.
- 업스트림 종료 확인 전에는 슬롯을 점유 상태로 유지한다.
- 정해진 정리 제한 시간 안에 종료되지 않으면 자식 프로세스를 종료하고 해당 대상을 비정상 상태로 전환한다.
- 자동으로 프로세스를 계속 재시작하여 Redis에 추가 요청을 쌓지 않는다. 초기 버전은 운영자 확인 후 재시작하는 방식을 제안한다.
- 프로세스 종료도 Redis 서버에서 이미 실행 중인 명령의 취소를 보장하지 않는다.

## 5. 도구 정책

아래는 초기 정책 제안이다. 실제 공개 목록은 대상 데이터 크기·민감정보 조사 후 확정한다.

| 공개 기능 | 공식 도구 매핑 | 초기 정책 | 비고 |
|---|---|---|---|
| `list_targets` | 없음 | 항상 제공 | 등록된 대상 메타데이터만 조회, Redis 호출 없음 |
| `inspect_key` | `type` | target별 선택 | 조사 당시 TYPE·TTL 실행. 원자적 결과는 아님 |
| `scan_keys_page` | `scan_keys` | 조건부 활성화 | SCAN의 비용·키 이름 노출을 허용할 때만 사용 |
| `list_length` | `llen` | 활성화 후보 | 결과가 작고 명령 비용을 예측하기 쉬움 |
| `read_list_page` | `lrange` | 기본 비활성화 | 개수·offset 제한과 원소 크기 검토 필요 |
| `read_sorted_set_page` | `zrange` | 기본 비활성화 | 개수·offset 제한과 member 크기 검토 필요 |
| 문자열·Hash 필드 조회 | `get`, `hget` | 기본 비활성화 | 단일 값 크기를 공식 도구에서 제한하지 못함 |
| 전체 키 탐색 | `scan_all_keys` | 금지 | 내부 반복과 결과 누적이 제한되지 않음 |
| 전체 컬렉션 조회 | `hgetall`, `smembers` | 금지 | 데이터 전체 읽기 |
| 쓰기·관리·스크립트·구독 | 관련 도구 전체 | 금지 | read-only 범위 외 |
| 문서 검색 등 외부 네트워크 도구 | 관련 도구 | 금지 | 진단 목적에 불필요한 외부 전송 경로 |

`LRANGE`는 반환 개수가 작아도 큰 offset에 따른 탐색 비용이 있다. 페이지 크기 외에 최대 시작 offset도 제한한다. List·Sorted Set 원소 크기는 반환 개수와 별개의 위험이다.

### 5.1 키 및 패턴 제한

- 단일 키는 운영자가 정한 literal prefix 또는 exact key와 비교한다.
- 탐색 API는 `scope_id`를 받는다. scope는 운영자 설정의 literal prefix로 매핑된다.
- prefix를 Redis glob 문법에 맞게 escape한 뒤 끝에 `*`를 붙인다.
- `*`, `?`, `[`, `]`, `\\` 및 유니코드 경계 등을 테스트한다.
- Redis 키는 binary-safe이지만 초기 프록시는 검증된 UTF-8 키 범위만 지원하는 방안을 제안한다. 바이너리 키는 조용히 변환하지 않고 지원 범위를 명시한다.
- SCAN 결과는 허용 prefix를 재검사한다. 이 검사는 결과 노출 제어이지 Redis 내부 탐색량 감소가 아니다.

### 5.2 SCAN 탐색 세션

원시 Redis cursor를 클라이언트에서 자유롭게 받지 않는다. 프록시가 탐색 상태를 보유하고 **추측 불가능한 불투명 토큰**을 반환한다.

상태에는 다음을 포함한다.

- 대상, scope, 정책 버전, Redis cursor.
- 누적 SCAN 호출 수, 관측 키 수, 시작 시각·만료 시각.
- 중복 키 추적 상태와 그 메모리 상한.
- 연결 세션 식별자, 다음 토큰, 진행 상태.

정책:

- 첫 요청과 모든 후속 페이지는 대상별 rate limit을 소비한다.
- 같은 토큰의 동시 사용은 거절한다. 토큰은 성공적인 상태 전이마다 교체한다.
- 만료·잘못된 대상·다른 세션·이전 정책 토큰은 거절한다.
- 빈 페이지도 한 번의 SCAN 실행이다. cursor가 0이 아니면 완료로 표시하지 않는다.
- 중복 키는 결과에서 제거할 수 있으나 관측량 예산에는 포함한다.
- 응답 초과로 한 페이지의 키를 일부 버린 경우 `truncated=true`로 표시하고 해당 탐색을 종료하는 단순한 정책을 제안한다. 남은 키를 조용히 누락한 채 완전한 순회를 주장하지 않는다.
- 만료 상태는 제거하고, 활성 탐색 세션 수와 상태 메모리에도 상한을 둔다.
- 프로세스 재시작 시 토큰은 무효화된다. 대상 rate limit의 재시작 초기 burst도 보수적으로 적용한다.

주의: SCAN은 MATCH가 좁아도 전체 키 공간을 순회할 수 있고 COUNT는 힌트다. 커서가 0이 된 순회도 변경 중인 데이터의 시점 일관성·중복 없음까지 의미하지 않는다. 단일 페이지의 엄격한 Redis 응답 바이트 상한이 필수이면 SCAN을 비활성화해야 한다.

### 5.3 선택적 대체 조회

문자열 미리보기가 필요하면 별도 도구 `read_string_preview`를 검토한다.

- `GET` 후 절단 대신 `GETRANGE`로 실제 Redis 반환 바이트를 제한한다.
- 시작 offset과 최대 바이트 수를 제한한다.
- UTF-8 중간 바이트 절단·binary 데이터 표현을 명확히 정의한다.
- 별도 Redis 연결을 사용한다면 동일 ACL·limiter·감사 정책을 반드시 통과시킨다.
- 초기 버전에서는 이 어댑터를 구현하지 않고 해당 조회를 차단해도 된다.

Hash 필드·List 원소·Sorted Set member는 문자열 미리보기와 같은 방식으로 일반화할 수 없다. 크기 사전 검사 후 읽는 방식도 중간 변경에 따른 경쟁 조건이 있으므로 엄격한 바이트 상한 보장으로 취급하지 않는다.

## 6. 정책 설정 모델

설정은 프로세스 소유이며 도구 호출로 변경할 수 없다. 외부 설정은 TOML v2이며, 완전한 두 대상 예시는 [policy.example.toml](../config/policy.example.toml)에 있다.

```toml
policy_version = 2
audit_path = "router.audit.jsonl"

[[targets]]
id = "catalog-cache"
environment = "local"
host = "127.0.0.1"
port = 6379
username = "catalog_reader"
password_env = "CATALOG_REDIS_PASSWORD"
tls = false
readonly_acl_confirmed = true
allowed_prefixes = ["example:catalog:"]
tools = ["inspect_key", "list_length"]
audit_path = "catalog.audit.jsonl"

[targets.limits]
max_inflight = 1
requests_per_second = 1.0

[[targets]]
id = "job-queue"
environment = "local"
host = "127.0.0.1"
port = 6380
username = "queue_reader"
password_env = "QUEUE_REDIS_PASSWORD"
tls = false
readonly_acl_confirmed = true
allowed_prefixes = ["example:jobs:"]
tools = ["list_length"]
audit_path = "queue.audit.jsonl"
```

target별 설정은 서로 상속하지 않는다. 생략한 제한값만 보수적인 기본값을 사용한다. 허용 키 범위·도구·접속 대상·계정·ACL 확인 선언은 명시해야 한다. 위 수치는 부하 테스트 전 예시이며 운영 권장값으로 확정하지 않는다.

최상위 입력·응답 상한은 공통 MCP 연결을 보호하고, target별 제한은 각 대상의 조회를 보호한다. target 수를 최대 16개로 제한한다. 동일 endpoint/DB의 직접 중복 등록은 거절하지만 DNS 별칭·서로 다른 DB의 물리 Redis 공유까지 자동 감지하지는 않는다.

SCAN의 페이지·시간·관측 키 수 및 토큰 정책은 향후 기능이다. 현재 설정에서 미구현 도구나 탐색 제한 필드를 지정하면 거절한다.

## 7. 결과와 오류 계약

공개 도구는 성공 데이터와 실행 상태를 구분한다. 구조화 결과 형태의 예시는 다음과 같다.

```json
{
  "data": {"keys": ["example:cache:item"]},
  "meta": {
    "partial": true,
    "truncated": false,
    "scan_complete": false,
    "stop_reason": "page_returned",
    "next_token": "opaque-token",
    "upstream_calls": 1
  }
}
```

- `partial`: 전체 대상의 일부만 관측했는지 여부.
- `truncated`: 이미 얻은 결과 일부를 제한 때문에 반환하지 못했는지 여부.
- `scan_complete`: cursor가 종료 상태인지 여부. 시점 일관성을 의미하지 않는다.
- `stop_reason`: 페이지 반환, 탐색 완료, 예산 소진, 크기 초과 등의 사유.
- `next_token`: 안전하게 이어서 조회할 수 있을 때만 제공한다.

조회 성공 응답의 `meta.target_id`에는 실제 조회한 등록 대상 ID를 포함한다.

오류는 `TARGET_UNKNOWN`, `TOOL_DENIED`, `INVALID_ARGUMENT`, `KEY_SCOPE_DENIED`, `RATE_LIMITED`, `BUSY`, `SCAN_BUDGET_EXCEEDED`, `CURSOR_INVALID`, `UPSTREAM_UNAVAILABLE`, `UPSTREAM_CONTRACT_MISMATCH`, `TIMEOUT`, `RESPONSE_TOO_LARGE` 등 안정적인 코드로 변환한다.

MCP 프로토콜 오류와 도구 실행 오류를 SDK 규약에 맞게 구분한다. 업스트림 오류 원문을 그대로 노출하지 않으며, 오류·timeout을 빈 정상 결과로 바꾸지 않는다.

## 8. 자격증명·ACL·배포

- 대상 환경을 확인하기 전 운영 연결 정보를 추정하거나 기본값으로 설정하지 않는다.
- 운영자가 사전 생성한 최소 권한 계정을 사용한다. 프록시가 계정을 생성하거나 ACL을 변경하지 않는다.
- 활성 도구가 실제 실행하는 명령과 연결 준비 명령을 기준으로 ACL을 생성·검토한다.
- SCAN은 ACL 키 범위만으로 이름 열거를 격리할 수 없으므로 별도 위험 승인이 필요하다.
- TLS 인증서 검증을 유지하고 인증 오류를 우회하지 않는다.
- 자격증명은 명령행·로그·저장소에 넣지 않는다. 프로세스 환경 등으로 주입할 때도 동일 OS 사용자의 접근 가능성을 고려한다.
- replica를 사용할 경우 최신성 지연과 복제 부하를 문서화한다. replica 연결도 무부하를 의미하지 않는다.
- Redis Cluster는 cursor·노드별 호출·부분 장애의 의미가 달라 별도 어댑터 및 예산 검증 전까지 지원하지 않는다.
- 원격 서비스로 확장하면 사용자 인증·인가, 사용자/대상별 제한, 공유 예산, secret 격리, 네트워크 통제를 추가 설계한다.

## 9. 테스트 전략

### 단위 테스트

- 도구 허용 목록, 직접 호출 우회, 추가 인자·음수·큰 정수·긴 문자열 거절.
- prefix 및 glob escape, 정책 설정 검증.
- 고정된 테스트 시계로 rate limit·토큰 만료·누적 탐색 예산 검증.
- 동시 요청과 토큰 재사용, 취소 후 슬롯 유지.
- UTF-8 바이트 계산, 구조화 응답 절단, 오류·로그 정제.

### 계약 테스트

- 고정한 공식 MCP 도구 스키마·이름·출력 형태 확인.
- 실제 Redis 명령과 필요한 ACL 권한 확인.
- 도구 설명·추가 capability·서버 주도 요청을 무조건 전달하지 않는지 검증.
- 업스트림이 텍스트 오류·비정상 JSON·과도한 프레임을 반환할 때 실패 처리 검증.

### 통합·부하 테스트

별도 테스트 Redis에서 합성 데이터만 사용한다.

- 많은 키, 긴 키, 큰 String, 큰 Hash 필드, 큰 List 원소, 큰 Sorted Set member.
- SCAN 빈 페이지·중복·동시 변경·예상보다 큰 반환 페이지.
- 네트워크 지연·연결 종료·업스트림 프로세스 비정상 종료.
- 병렬 MCP 호출, 반복 timeout, 여러 탐색 세션 생성.
- 쓰기·관리·스크립트 명령의 Redis ACL 차단.
- 제한 전후 Redis latency·CPU·네트워크와 프록시 메모리 비교.

mock만으로 Redis 부하를 검증했다고 주장하지 않는다. 운영 도입 기준은 실제 대상과 유사한 데이터 분포에서 별도로 정한다.

## 10. 단계별 도입

### 단계 A: 범위 확정

환경·버전·데이터 크기·값 조회 필요성·허용 prefix·운영 지연 허용치를 확인한다. 공식 MCP 버전과 commit SHA를 고정한다.

### 단계 B: 최소 프록시

stdio 업스트림 연결, `inspect_key` 등 소수 도구, 정책 검증, ACL, limiter, 감사, 계약 테스트를 구현한다. SCAN과 값 조회는 기본 비활성화한다.

### 단계 C: 제한된 탐색

실제 필요할 때만 탐색 토큰·누적 예산·결과 필터링을 구현하여 SCAN을 활성화한다. 조회 구간 도구도 데이터 크기 검증 후 개별 활성화한다.

### 단계 D: 부하 검증 및 제한적 운영 도입

합성 부하 테스트 통과 후 승인된 대상에 낮은 호출률로 적용한다. Redis 영향이 기준을 넘으면 도구를 비활성화하거나 프록시를 중지한다. 자동 정책 완화는 하지 않는다.

### 단계 E: 필요한 기능만 확장

다중 대상 등록·라우팅은 현재 구현에 포함한다. 문자열 미리보기, 원격 서비스, Cluster 등은 각각 별도 요구·위험 검토 후 추가한다.

## 11. 설계 한계와 대안

- 공식 도구를 거의 모두 차단하고 대부분 대체하게 된다면, 공식 MCP 중계의 이점이 줄어든다. 그 시점에는 작은 전용 MCP로 전환하는 편이 단순할 수 있다.
- 공식 MCP의 동기 Redis 호출은 취소 전파와 실행 시간을 제어하는 데 제약이 있다. 프록시 deadline만으로 해결되지는 않는다.
- 네트워크·메모리의 엄격한 상한이 필요하면 해당 명령의 응답 자체를 제한할 수 있어야 한다. 불가능한 명령은 차단해야 한다.
- 운영 주 Redis에 직접 읽는 대신 별도 진단용 데이터로 복제·집계하는 방식은 격리를 강화하지만 최신성·운영 비용이 증가한다.

## 12. 참고 자료

조사 당시 공식 저장소의 `main` 소스를 참고했다. 아래 링크는 변경 가능한 참조이며 구현 시작 시 고정 commit 링크로 대체하고 계약 테스트 결과를 기록한다.

- [공식 Redis MCP](https://github.com/redis/mcp-redis)
- [SCAN 및 전체 탐색 구현](https://github.com/redis/mcp-redis/blob/main/src/tools/misc.py)
- [String 조회 구현](https://github.com/redis/mcp-redis/blob/main/src/tools/string.py)
- [Hash 조회 구현](https://github.com/redis/mcp-redis/blob/main/src/tools/hash.py)
- [List 조회 구현](https://github.com/redis/mcp-redis/blob/main/src/tools/list.py)
- [Set 조회 구현](https://github.com/redis/mcp-redis/blob/main/src/tools/set.py)
- [Sorted Set 조회 구현](https://github.com/redis/mcp-redis/blob/main/src/tools/sorted_set.py)
- [연결 설정](https://github.com/redis/mcp-redis/blob/main/src/common/connection.py)
- [Redis ACL](https://redis.io/docs/latest/operate/oss_and_stack/management/security/acl/)
- [Redis SCAN](https://redis.io/docs/latest/commands/scan/)
- [Redis GETRANGE](https://redis.io/docs/latest/commands/getrange/)
- [Redis LRANGE](https://redis.io/docs/latest/commands/lrange/)
