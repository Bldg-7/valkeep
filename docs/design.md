# Valkeep 설계

Valkeep 0.4는 **Valkey 우선의 다중 대상 읽기 전용 로컬 stdio MCP**다. Redis는 engine=redis를 명시하는 호환 모드다. [실행 안내](implementation.md)와 [설정 예시](../config/policy.example.toml)를 함께 참고한다.

## 1. 전체 구조

```text
AI 클라이언트
  │ 공개 MCP / stdio
  ▼
Valkeep 라우터
  ├─ 도구 계약·명시적인 target_id·공통 입력/응답 상한·라우터 감사
  ├─ target A: 정책·limiter·탐색 상태·감사·수명 태스크
  │    └─ Valkeep worker A → 공식 valkey-py → 전용 ACL 계정 → Valkey A
  └─ target B: 정책·limiter·탐색 상태·감사·수명 태스크
       └─ Valkeep worker B → 공식 valkey-py → 전용 ACL 계정 → Valkey B
```

0.3까지 사용한 Redis 공식 MCP 의존성은 제거했다. 별도 서버에 도구를 덧붙이는 대신 Valkeep 전용 worker에 검토한 읽기 도구만 등록한다. worker와 공개 MCP 사이의 구분은 target별 실행 중단·자격증명 분리와 메시지 계약 검증을 위해 유지한다.

## 2. 엔진·프로토콜·지원 범위

- 연결 client는 공식 valkey-py 6.2.0, MCP SDK는 1.30.0으로 고정한다.
- target.engine은 valkey 또는 redis이며 기본값은 valkey다.
- 최소 버전 정책은 Valkey 8.1, Redis 호환 모드 7.2다.
- 데이터 프로토콜은 RESP2로 고정하고 standalone만 지원한다.
- 관리형 서비스·Cluster·Sentinel·서버 위조 방지를 위한 별도 증명은 범위 밖이다.

client의 인증·TLS·DB 선택 이후 HELLO 2를 실행하여 server·version·proto·mode를 검증한다. 설정과 실제 엔진이 다르면 ENGINE_MISMATCH, 버전 미달은 UNSUPPORTED_SERVER_VERSION, 지원하지 않는 topology는 UNSUPPORTED_TOPOLOGY다. 정상 데이터 명령 전에 연결 단계에서 실패하며 재연결에서도 검사한다.

HELLO 결과는 server가 제공하는 정보다. TLS 인증서와 hostname 검증을 대체하지 않는다. 인식할 수 없는 정보로 임의의 Redis 호환 서버를 자동 승인하지 않는다.

## 3. 설정 및 프리셋

policy_version=2와 최대 16개의 [[targets]] 목록을 사용한다. 정책은 프로세스 소유이며 도구 호출로 변경할 수 없다. 모든 조회에 target_id를 필수로 받는다.

production-safe는 메타데이터·List 길이·연결 점검만 제공하며 값 미리보기·탐색은 features로 각각 켠다. 제한 상한을 검증하며, 고급 tools 설정은 custom 경로로 분리한다. 기존처럼 preset 없이 tools를 명시하면 custom으로 해석한다.

target마다 연결·자격증명·도구·키 범위·제한·감사를 분리한다. 중복 ID·직접 중복 endpoint/DB·감사 파일 경로 충돌을 거절한다. DNS 별칭과 logical DB의 물리 자원 공유까지 자동 감지하지 않는다.

기존 Redis target은 engine=redis를 명시해야 한다. 소켓 제한의 새 이름은 socket_timeout_seconds이며 redis_timeout_seconds는 입력 별칭으로만 유지한다. 모호한 이중 설정은 거절한다.

## 4. 수명 및 신뢰 경계

target마다 별도 asyncio 태스크가 MCP ClientSession과 자식 프로세스 수명을 소유한다. 시작 단계에서 모든 설정·비밀번호 존재·필요 도구 계약을 검증한다. 하나라도 실패하면 시작한 프로세스를 정리하고 공개 서버 시작을 거절한다.

Redis/Valkey 연결은 첫 요청 시 생성된다. worker 초기화 완료를 서버 연결 정상으로 표현하지 않는다. 실행 중 한 target의 오류·지연·대상 감사 실패는 다른 정상 target과 분리한다. 공통 라우터 감사 실패는 모든 새 조회를 차단한다.

자식은 선택한 비밀번호와 정책만 환경변수로 받으며 부모의 임의 환경변수·경로를 상속하지 않는다. 인증정보를 명령행에 넣지 않는다. 동일 OS 사용자의 프로세스 접근을 완전히 격리하거나 사용자별 target 권한을 분리하는 서비스는 아니다.

## 5. 조회 계약

| 공개 도구 | 실제 명령 | 경계 |
|---|---|---|
| list_targets | 로컬 상태만 조회 | ID·engine·환경·도구·scope ID, 접속 정보 미노출 |
| inspect_key | TYPE, PTTL, 타입별 길이 명령 | 최대 3개 데이터 명령, 값 미조회 |
| list_length | LLEN | 키 범위 재검증, 별도 외부 MCP 사용 안 함 |
| connection_status | 연결 협상 후 PING | 인증·엔진 확인, ACL 전체 검증은 아님 |
| preview_key | TYPE, STRLEN, GETRANGE | String만, 앞부분 바이트 상한 |
| find_keys | SCAN 1회 | literal prefix·힌트·누적 탐색 예산 |

입력과 출력 스키마를 공개 서버·worker 경계에서 검증한다. 등록되지 않은 도구를 직접 호출해도 실행하지 않는다. worker도 정책의 도구·키·preview limit·scan prefix/COUNT를 재검사한다.

resources·prompts·sampling·elicitation은 외부로 중계하지 않는다. 불필요한 쓰기·관리·구독 도구 자체를 worker에 등록하지 않는다. 범용 execute_command 인터페이스는 제공하지 않는다.

## 6. 메타데이터 및 미리보기

타입별 길이는 STRLEN/LLEN/HLEN/SCARD/ZCARD/XLEN으로 구한다. 미지원 타입은 길이 null을 반환한다. 여러 명령 간 데이터 변경을 허용하되 atomic=false를 명시하고 타입 변경 오류를 빈 정상 결과로 바꾸지 않는다.

String 미리보기는 정책 한도 내에서 GETRANGE 0 N-1을 실행한다. 기본 1KB·최대 4KB이며 전체 GET 후 사후 절단하지 않는다. 잘린 UTF-8·바이너리는 base64로 표현한다. Hash 필드·List 원소 등의 큰 값 문제를 우회하여 전체 조회하는 경로는 없다.

## 7. 탐색 상태 및 예산

find_keys는 target_id, scope_id와 선택적인 일회용 token만 받는다. 사용자가 cursor·pattern·COUNT를 직접 조작할 수 없다. prefix의 glob 특수문자를 escape하고 반환 키도 어댑터와 부모에서 검사한다.

내부 상태에는 target별 scope·prefix·cursor·만료·누적 호출/관측·중복 집합·실행 중 여부를 보관한다. 토큰은 난수이며 대상·scope·현재 프로세스에 묶인다. 호출률·슬롯·감사 확보 뒤 토큰을 소비하고 성공적인 다음 페이지에만 새 토큰을 발급한다.

빈 페이지·중복 키도 예산을 소비한다. 활성 상태 수와 중복 집합 크기를 제한한다. cursor 종료, 호출/관측/시간 예산, 결과 생략·오류 때 상태를 제거한다. partial, truncated, scan_complete, stop_reason, next_token으로 종료 사유를 표현한다.

SCAN COUNT는 힌트여서 한 페이지가 관측 예산·예상 크기를 넘을 수 있다. 마지막 페이지의 실제 관측 합계를 숨기지 않고 탐색을 종료한다. 결과 일부를 버리면 계속 조회해도 전체 결과인 것처럼 주장하지 않는다. MATCH와 ACL 키 범위는 내부 탐색 비용의 엄격한 격리가 아니다.

## 8. 부하·timeout·감사

target별 token bucket과 실행 슬롯으로 대기열 없이 제한한다. MCP 한 호출은 여러 서버 명령을 포함할 수 있어 CPU·실제 명령 수의 정확한 상한은 아니다.

공식 client에 socket/connect timeout, retry=0을 지정한다. 응답 timeout·취소와 실제 실행 수명을 분리하고 종료 확인 전까지 슬롯을 유지한다. 정리 시간을 넘으면 해당 자식을 종료하고 target을 차단한다. 이미 서버에서 실행 중인 명령의 취소를 보장하지는 않는다.

공개 입력·worker 프레임·최종 결과 크기를 별도로 검사한다. 감사는 target·라우터별 owner-only 파일로 기록하고 symlink·다른 소유자·공유 권한을 거절한다. 원본 키·값·비밀번호·토큰·worker 오류 원문은 기록하지 않는다. worker stderr는 폐기한다.

## 9. ACL 프로필

도구에서 필요한 개별 명령을 계산하고 -@all 및 %R~ 키 범위를 사용한다. CLIENT SETINFO 하위 명령만 연결 정보 등록용으로 허용하고, 0이 아닌 DB에는 SELECT를 추가한다. HELLO/AUTH는 연결 협상·인증 명령이며 이를 위해 +@connection 전체를 열지 않는다.

권한안에는 engine과 minimum_version을 포함한다. 현재 공통 명령 집합을 사용하지만 프로필과 최소 버전을 엔진별로 구분하고 실제 Valkey 두 버전에서 검사한다. category 이름·멤버십에 의존해 광범위 권한을 허용하지 않는다.

계획을 자동 적용하지 않는다. reset이 기존 권한·비밀번호를 지우며 비밀번호는 별도 설정해야 함을 경고한다. 관리자 ACL GETUSER JSON 오프라인 검사에서 추가 selector·채널·쓰기·불일치 키 범위를 거절한다. export 검사는 현재 실제 계정의 권한이나 파일 출처를 증명하지 않는다.

## 10. 검증 매트릭스

- Valkey 8.1.6, 9.1.2: digest가 고정된 공식 Docker 이미지, 평문·TLS 모두 실행.
- Redis 8.10.1: 설치된 native 서버로 명시적인 호환 모드 회귀 검사.
- 기본 테스트는 valkey-server가 설치되어 있으면 native Valkey를 사용할 수도 있다.
- --require-valkey는 Valkey/TLS 필수 조건이 없으면 skip 대신 실패한다.
- CI는 두 Valkey 버전을 별도 job으로 실행하도록 구성한다.

임시 서버·계정·인증서·합성 데이터만 사용한다. 랜덤 host 포트는 loopback에만 공개하고 테스트 컨테이너는 종료 시 제거한다. 실제 테스트는 INFO server로 Valkey임을 확인한 후 데이터 검증을 시작한다. 외부 서버 URL을 받지 않는다.

## 11. 한계와 운영 책임

최소 버전 검사는 모든 배포의 호환성 인증이 아니다. 실제 인증 방식·관리형 프록시의 HELLO 지원·데이터 분포·부하·감사 보존·자격증명 회전은 운영 전에 검증해야 한다. 원격 다중 사용자 인증·Cluster·Sentinel·IAM은 현재 범위 밖이다.

## 12. 참고 자료

- [Valkey Python client](https://github.com/valkey-io/valkey-py)
- [Valkey ACL](https://valkey.io/topics/acl/)
- [Valkey HELLO](https://valkey.io/commands/hello/)
- [Valkey SCAN](https://valkey.io/commands/scan/)
- [Valkey GETRANGE](https://valkey.io/commands/getrange/)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)

실행 의존성은 valkey 6.2.0, MCP SDK 1.30.0 및 uv.lock의 배포 해시로 고정한다.
