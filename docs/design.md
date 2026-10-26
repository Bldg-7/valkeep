# Valkeep 설계

현재 구현은 다중 standalone Redis용 로컬 stdio MCP다. 실제 설정과 실행 절차는 [구현·실행 안내](implementation.md)를 참고한다. 운영 부하 허용치와 managed service 호환성은 별도 검증 대상이다.

## 1. 전체 구조

```text
AI 클라이언트
  │ MCP / stdio
  ▼
Valkeep
  ├─ 공개 도구 계약·target 라우터
  ├─ 공통 입력·출력 상한 및 라우터 감사
  ├─ target A: 정책·limiter·탐색 상태·감사·수명 태스크
  │    └─ 공식 Redis MCP 자식 A
  │         ├─ 공식 LLEN 도구
  │         └─ Valkeep 읽기 어댑터 → 전용 ACL 계정 → Redis A
  └─ target B: 정책·limiter·탐색 상태·감사·수명 태스크
       └─ 공식 Redis MCP 자식 B → 전용 ACL 계정 → Redis B
```

공식 서버를 fork하지 않는다. 고정 버전의 서버를 실행하면서 제한된 `valkeep_read` 내부 도구를 추가한다. 이 도구는 AI 클라이언트에 직접 노출하지 않는다. 공식 서버의 다른 도구도 자동 공개하지 않는다.

## 2. 책임 분리

| 계층 | 책임 |
|---|---|
| 공개 MCP 서버 | 검토한 도구의 이름·설명·입력 스키마만 노출 |
| 라우터 | 명시적인 target_id 검사, 대상 선택, 공통 감사 |
| 대상 정책 | 허용 도구·키 범위·기능·제한값 검사 |
| 실행 제어 | 호출률·동시 실행·timeout·실제 실행 수명 관리 |
| 탐색 상태 | opaque token, 누적 호출·관측·시간·상태 개수 제한 |
| 공식 MCP 및 읽기 어댑터 | 검토한 Redis 명령만 실행, 제한된 값 조회 |
| Redis ACL | 데이터 변경·불필요한 명령·키 접근의 서버 측 차단 |

ACL은 호출률·명령 인자·SCAN 순회량을 제한하지 않는다. 반대로 프록시의 도구 필터만으로 실제 Redis 계정이 read-only임을 보증하지 않는다.

## 3. 구성 및 프리셋

외부 설정은 `policy_version = 2`, `[[targets]]` 목록이다. 최대 16개 target을 허용한다. 설정은 프로세스 소유이며 도구 호출로 바꿀 수 없다.

- `production-safe`는 메타데이터·List 길이·PING 점검만 기본 허용한다.
- `features.value_preview`, `features.key_discovery`로 추가 기능을 각각 켠다.
- 명시적인 `tools`는 고급 custom 경로이며 features와 함께 사용하지 않는다. 기존 설정처럼 preset이 없으면 custom으로 해석하고, production-safe와 tools를 함께 지정하면 거절한다.
- 프리셋의 호출률·동시 실행·탐색 예산 상한을 넘으면 시작을 거절한다.
- endpoint·계정·허용 키 범위·감사 경로·관리자의 ACL 확인 선언은 명시해야 한다.
- 중복 ID, 같은 host/port/database 조합, 감사 파일 경로 충돌을 거절한다.
- 다른 DNS 별칭이나 다른 logical DB가 같은 Redis 자원을 공유하는지는 자동 감지하지 않는다.

설정 예시는 [policy.example.toml](../config/policy.example.toml)에 있다. 하나의 target만 있어도 조회에서 target_id를 생략할 수 없다.

## 4. 대상별 수명과 장애 격리

각 target의 별도 asyncio 태스크가 해당 MCP ClientSession과 자식 프로세스의 생성·종료를 소유한다. 여러 세션의 취소 범위를 하나의 태스크에 중첩하지 않는다.

시작 순서:

1. 모든 설정과 필요한 비밀번호 환경변수 존재를 검증한다.
2. target별 감사 파일과 공식 MCP 프로세스를 생성한다.
3. 고정 버전·필요 도구 입력 계약을 검증한다.
4. 하나라도 실패하면 시작한 프로세스를 정리하고 공개 MCP 시작을 거절한다.
5. 모두 성공하면 공개 MCP를 시작한다. 실제 Redis 연결은 첫 데이터/연결 점검 요청 시 생성된다.

실행 중 한 target의 지연·프로세스 비정상 종료·감사 오류는 그 target을 차단한다. 다른 정상 target은 계속 사용할 수 있다. 공통 라우터 감사 실패는 모든 새 조회를 차단한다.

등록된 모든 target은 동일 로컬 MCP 사용자가 접근할 수 있다. 사용자별 권한 분리나 강한 동일 OS 사용자 격리를 제공하는 구조는 아니다.

## 5. 공개 API와 실제 명령

| 공개 도구 | 내부 실행 | 비용·제약 |
|---|---|---|
| list_targets | 로컬 상태만 조회 | Redis 명령 없음, 접속 정보 미노출 |
| inspect_key | TYPE, PTTL, 타입별 길이 명령 | 최대 3개 명령, 값은 반환하지 않음 |
| list_length | 공식 LLEN 도구 | 값 조회 없음 |
| connection_status | PING | 현재 연결·인증 점검, ACL 전체 검증 아님 |
| preview_key | TYPE, STRLEN, GETRANGE | String만, 앞부분 바이트 한도 강제 |
| find_keys | SCAN 1회 | COUNT 힌트·prefix·누적 예산 적용 |

타입별 길이는 STRLEN/LLEN/HLEN/SCARD/ZCARD/XLEN으로 구한다. 모듈 타입 등 길이 조회를 지원하지 않는 타입은 길이 null을 반환한다.

여러 명령을 순서대로 실행하므로 메타데이터와 미리보기는 `atomic=false`다. 타입 변경으로 발생한 명령 오류를 정상적인 빈 데이터로 바꾸지 않는다.

## 6. String 미리보기

사용자에게 byte limit·offset을 임의 인자로 받지 않는다. 정책의 `preview_bytes`를 사용하여 `GETRANGE 0 N-1`만 실행한다.

- 기본 1,024바이트, 설정 가능한 상한 4,096바이트.
- String이 아니면 UNSUPPORTED_TYPE으로 거절한다.
- UTF-8이 아니거나 문자 경계에서 끊기면 base64로 반환한다.
- 관측된 전체 길이가 더 크거나 조회 한도에 도달하면 truncated=true로 표시한다.
- 크기 관측과 미리보기 사이의 변경을 허용하되 원자적 결과로 주장하지 않는다.
- 값은 로그에 기록하지 않고, 도구 설명에서 신뢰할 수 없는 데이터임을 명시한다.

Hash 필드·List 원소 등의 값은 개수만으로 바이트 상한을 보장할 수 없으므로 현재 미리보기에서 지원하지 않는다. 전체 GET 후 사후 절단하는 경로도 없다.

## 7. 키 탐색과 토큰

### 입력과 범위

`find_keys`는 target_id, scope_id, 선택적인 token만 받는다. scope_id는 설정의 literal prefix에 대응한다. 사용자에게 pattern·cursor·COUNT를 받지 않는다.

prefix의 Redis glob 특수문자를 escape하고 끝에만 `*`를 붙인다. 어댑터와 부모 모두 반환 키의 prefix를 확인한다. 키는 유효한 UTF-8과 설정된 바이트 상한을 만족해야 한다.

### 상태와 예산

target별 Scans 객체가 다음을 보유한다.

- 내부 세션 ID, scope와 prefix, 만료 시각, Redis cursor.
- 누적 SCAN 호출 수, 관측 키 수, 중복 제거 집합.
- 현재 처리 중 여부와 외부 일회용 토큰 매핑.

토큰은 추측 불가능한 난수다. 호출률·실행 슬롯과 시작 감사가 확보된 뒤 토큰을 소비하며, 성공한 다음 페이지에만 새 토큰을 발급한다. 같은 토큰의 동시 사용·재사용·다른 scope/target 사용은 거절한다. 재시작하면 기존 토큰은 무효다.

빈 페이지도 한 번의 호출로 계산하며 중복 키도 관측 예산에 포함한다. COUNT 힌트를 남은 예산 이내로 낮추지만 Redis가 더 반환할 수 있으므로, 실제 관측 합계는 마지막 페이지에서 예산을 초과할 수 있다. 초과 여부를 숨기지 않고 집계한 뒤 탐색을 종료한다. 활성 세션 수와 관측 집합 크기도 제한한다. 만료된 비실행 상태는 다음 요청에서 정리한다.

### 종료 및 부분 결과

다음 경우 이어받기 토큰을 발급하지 않고 상태를 제거한다.

- cursor가 0이 되어 순회 종료.
- 호출·관측·시간 예산 도달.
- 큰 키·바이너리 키·범위 밖 키 등의 결과 생략.
- 클라이언트 응답 크기 초과로 키를 일부 제거.
- 업스트림 오류·실행 정리 실패.

응답에 partial, truncated, scan_complete, stop_reason, next_token, 누적 calls/observed를 제공한다. cursor가 0이어도 변경 중인 DB의 시점 일관성은 보장하지 않는다.

**SCAN COUNT는 엄격한 반환 개수·바이트 제한이 아니다.** 한 Redis 응답의 자원 사용까지 절대 상한이 필요하면 탐색 기능을 비활성화한다. 키 prefix 필터 역시 Redis 내부 탐색 범위를 prefix만으로 제한하지 않는다.

## 8. timeout과 실행 예산

- 대상별 token bucket과 실행 슬롯으로 대기열 없이 요청을 제한한다.
- MCP 호출 하나가 여러 Redis 명령을 수행할 수 있으므로 호출률은 Redis 명령 수의 정확한 상한이 아니다.
- Redis client는 socket/connect timeout과 retry=0을 사용한다. 공식 LLEN도 같은 연결 설정을 사용한다.
- 클라이언트 응답 timeout·취소와 실제 실행 수명을 분리한다.
- 응답 대기가 끝났어도 업스트림 호출 종료 전에는 슬롯을 반환하지 않는다.
- 정리 제한 시간까지 호출이 남으면 해당 자식 프로세스를 종료하고 target을 차단한다.
- 이미 Redis에서 실행 중인 명령이 프로세스 종료로 반드시 중단되는 것은 아니다.

공개 입력, 업스트림 줄 프레임, 직렬화된 도구 결과 크기를 각각 제한한다. 응답 제한은 이미 Redis에서 읽은 데이터 비용을 되돌리지 못하므로 실제 명령 단계의 제한과 별도로 취급한다.

## 9. ACL 계획과 점검

허용 도구에서 필요한 Redis 명령을 도출한다. 권한안은 `reset on -@all` 뒤에 필요한 개별 명령과 `%R~` 키 패턴을 붙이는 JSON 인자 목록이다. CLIENT SETINFO를 지원하는 Redis 7.2 이상을 기준으로 한다.

- 관리자 권한이나 비밀번호를 MCP에 추가로 제공하지 않는다.
- ACL SETUSER를 자동 실행하지 않는다.
- 비밀번호는 권한안에 포함하지 않는다. reset이 기존 비밀번호와 권한을 지운다는 경고를 제공한다.
- `--verify-acl`은 관리자가 제공한 ACL GETUSER JSON을 오프라인으로 검사한다.
- 추가 selector·채널·광범위 카테고리·쓰기 명령·일치하지 않는 키 범위를 거절한다.
- 원본 ACL export나 비밀번호 해시는 출력·로그에 남기지 않는다.

오프라인 검사는 파일이 설정과 일치하는지 확인할 뿐, 그 파일이 특정 서버의 현재 계정에서 나온 것인지 증명하지 않는다. PING 연결 점검도 읽기 전용 ACL 검증이 아니다. 실제 운영 키에 쓰기 요청을 보내 검사하지 않는다.

## 10. 감사와 오류

라우터 및 각 target의 감사 파일을 분리한다. target ID, 정책 fingerprint, 요청 ID, 공개 도구명, 실행 결과, 지연시간·응답 크기를 기록한다. 원본 키·값·토큰·접속 URL·비밀번호·업스트림 오류 원문은 기록하지 않는다.

파일은 owner-only로 생성하고 symlink·공유 권한·다른 소유자 파일을 거절한다. 완료 감사가 실패하면 조회가 이미 실행되었더라도 성공으로 보고하지 않는다. 업스트림 stderr는 폐기하고 SDK의 원본 예외 로깅을 비활성화한다.

공개 오류는 TOOL_DENIED, TARGET_UNKNOWN, INVALID_ARGUMENT, KEY_SCOPE_DENIED, RATE_LIMITED, BUSY, PERMISSION_DENIED, UNSUPPORTED_TYPE, KEY_CHANGED, CURSOR_INVALID, SCAN_BUDGET_EXCEEDED, TIMEOUT, UPSTREAM_UNAVAILABLE, UPSTREAM_CONTRACT_MISMATCH, RESPONSE_TOO_LARGE, AUDIT_UNAVAILABLE 등 정해진 코드만 사용한다.

## 11. 검증과 남은 운영 책임

단위 테스트, 실제 공식 MCP 프로세스 테스트, 격리된 로컬 Redis 평문/TLS 테스트를 함께 사용한다. 실제 Redis에는 합성 데이터만 적재하며 권한 거절 테스트도 새로 생성한 로컬 계정으로만 수행한다.

검증 범위에는 주요 타입의 메타데이터, 큰 String의 실제 GETRANGE 제한, 바이너리 값, literal glob prefix, 탐색 토큰 재사용 방지·예산, 대상 간 장애 격리, 실제 쓰기/관리 권한 거절, 신뢰되지 않는 TLS 인증서 거절이 포함된다.

운영 환경의 지연·CPU·네트워크 허용치, 실제 계정 export의 신뢰 경로, 민감정보 범위, 감사 로그 회전·보관, 자격증명 회전은 별도 운영 책임이다. Cluster·Valkey·HTTP·다중 사용자 인가는 현재 지원 범위 밖이다.

## 12. 참고 자료

- [공식 Redis MCP](https://github.com/redis/mcp-redis)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Redis ACL](https://redis.io/docs/latest/operate/oss_and_stack/management/security/acl/)
- [Redis SCAN](https://redis.io/docs/latest/commands/scan/)
- [Redis GETRANGE](https://redis.io/docs/latest/commands/getrange/)
- [Redis PTTL](https://redis.io/docs/latest/commands/pttl/)

실행 의존성은 PyPI redis-mcp-server 0.5.1, MCP SDK 1.30.0, redis-py 8.1.0 및 uv.lock의 배포 해시로 고정한다.
