# Unreal Auto QA — 개발 인수인계 PRD

기준일 2026-11-11 · 대상 Windows x64 / Unreal Engine 5.9.0 / Win64 Development

## 1. 다음 작업부터 시작하기

**제품 방향 변경:** 사용자 테스트는 UE 활성 매핑 → OpenAI Decisions Choice → UE 입력·관측 루프로 실행한다. 수동 키·노드·시나리오 작성 및 재생은 사용자 실행 경로에서 제거한다. 아래의 기존 수동 테스트 요구와 CLI 예시는 과거 검증 범위다. [현재 실행 방법과 Decisions 선택 근거](README.md)

**2026-11-12 진행 기록:** G0와 G1을 통과했다. 패키징된 샘플에서 비활성 창의 전진·점프·동시 입력·홀드 중단을 확인했다. 실제 사용자 타이핑 중 QA 키 유입과 포커스 탈취는 관찰되지 않았다. G2의 FKey 입력 경로로 `UGameViewportClient::InputKey`를 채택한다. [실행 결과](spikes/background_input/README.md#2026-11-12-실행-결과)

G2의 실제 게임 1,000명령·중복 제출·Controller 강제 종료 시험을 통과했다. 실게임 시나리오, 단일 입력 Replay, 오프라인 Choice, Windows 배포와 두 번째 프로젝트의 기본 입력도 확인했다. 각 게이트의 검증 범위와 수동 단계는 [실제 UE 브리지 검증 기록](docs/g2-bridge.md)에 있다.

실게임 리매핑 W→I와 오래된 revision 거부를 확인했다. 승인된 Decisions 실호출 1회는 실제 JUMP와 키 해제를 완료했다.

**인수인계 시점에는 Python Controller와 Windows 모의 QA 앱이 구현되어 있었다.** G0–G1에서 별도 Third Person C++ 샘플과 입력 PoC를 사용한다. 모의 서버의 PASS는 실제 게임의 입력·IMC·포커스 독립성을 입증하지 않는다.

1. Windows PC에서 **이 폴더를 작업 폴더로 열고** UE **5.9.0**, MSVC x64, Windows SDK, 별도 Third Person **C++** 샘플 `.uproject`를 확인한다. 엔진은 있는데 샘플이 없으면 새 Third Person C++ 프로젝트를 만든다. 최종 게임과 최종 키 배정은 G0–G1의 선행 조건이 아니다.
2. 먼저 아래 자동 진단을 실행한다. 엔진 자동 탐지가 실패했거나 샘플 경로를 아직 받지 못했다면 필요한 경로만 확인한다. `-RequireReady`의 종료 코드 2는 준비 항목 부족을 뜻한다. 엔진이나 샘플을 확보할 수 없으면 UE 검증을 `BLOCKED`로 기록하고 오프라인 기준선까지 확인한다. 종료 코드 0은 파일 탐지 결과이며 컴파일 성공은 별도 검증한다.
3. 설치된 5.9.0 헤더에서 Enhanced Input, Viewport 입력, HTTP 서버 후보의 선언과 모듈 의존성을 확인한다. 샘플을 수정하기 전에 기본 Win64 Development 패키징을 먼저 성공시키고, 사용한 `RunUAT.bat BuildCookRun` 인자와 로그를 남긴다. 정확한 인자는 해당 설치본의 `BuildCookRun -Help` 및 실제 성공 로그를 기준으로 한다.
4. `spikes/background_input/`에 최소 C++ 입력 PoC와 실험 기록을 만든다. 샘플 게임에 필요한 변경은 경로와 변경 내용을 먼저 보여주고 승인받는다. **비활성 창 검증에 통과한 입력 경로를 G2의 `GameQABridge`에 채택한다.** 통과 전에는 원격 모델·리포트 기능 확장보다 입력 경로 검증을 우선한다.

```powershell
# 먼저 자동 탐지한다. 샘플 미선택 시 UPROJECT가 표시될 수 있다.
./spikes/check_environment.ps1
# 엔진과 샘플을 확인한 뒤 예시 경로를 실제 경로로 바꿔 실행한다.
./spikes/check_environment.ps1 -EngineRoot 'D:/UE_5.9' -Project 'D:/QaSample/QaSample.uproject' -RequireReady
python -B -m unittest -v
python -B spikes/decisions_smoke.py --self-test
```

현재 환경에서 발견한 도구 버전과 헤더 후보는 [UE 5.9.0 호환성 기록](docs/compatibility-ue59.md)에 있다. 공개 문서는 탐색에 사용하고, 구현 근거는 설치본 헤더·컴파일·패키징·실행 결과에 둔다. [Epic Enhanced Input](https://dev.epicgames.com/documentation/en-us/unreal-engine/enhanced-input-in-unreal-engine), [Epic 프로젝트 패키징](https://dev.epicgames.com/documentation/en-us/unreal-engine/packaging-your-project)

Windows PowerShell이 스크립트 실행을 차단하면 진단 스크립트의 내용을 확인한 뒤 해당 프로세스에서만 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\spikes\check_environment.ps1`로 실행할 수 있다. 시스템 실행 정책을 변경할 필요는 없다.

## 2. 제품 목표와 지켜야 할 경계

- 개발자가 평소처럼 편집기나 브라우저를 쓰는 동안 **별도 Development 게임 프로세스**에서 QA를 실행한다. 기본 입력 검사는 API 키 없이 사용할 수 있다. 자동 탐색은 사용자가 선택할 때만 OpenAI Decisions를 사용한다.
- 최종 P0 경로는 **Windows QA 앱 → Python Controller → 인증된 `127.0.0.1` HTTP → UE C++ `GameQABridge`**다. 대시보드와 게임 브리지는 별도 포트와 별도 인증을 사용한다. UE Runtime MCP는 후속 선택 사항이다.
- 입력은 QA 게임 프로세스 내부에서 처리한다. 운영체제 전역 입력 주입, 포커스 강제 이동, 화면 클릭 자동화, 임의 콘솔·셸 실행을 QA 경로에 넣지 않는다. Shipping에는 QA 입력 서버나 수신 경로가 열리지 않아야 한다.
- 프로젝트는 먼저 진단하고 변경 목록·해시·백업 경로를 표시한다. 승인 후에만 실제 프로젝트 파일을 바꾼다. 기존 Source·Content·Config·세이브와 사용자가 수정한 플러그인을 보존한다. 외부 프로그램 설치, 게임별 성공 조건 승인, API 키 등록은 해당 사용자에게 맡긴다.
- **API 키 값을 직접 읽어 확인하거나 복사·출력하지 않는다.** 사용자가 `Register-OpenAI-Key.cmd`에서 등록하고, Windows 사용자 범위 DPAPI로 보관한다. 실호출은 앱의 기존 인증 경로로만 하며 키는 HTTPS 인증 헤더에만 사용한다. 브라우저·모델 입력·UE 프로세스·로그·리포트·배포물로 키를 넘기지 않는다. 테스트 기본값은 오프라인이며 `--live`는 요청이 명시적으로 승인된 경우에만 사용한다.

## 3. 현재 구현과 검증 결과

| 영역 | 현재 동작 | 검증 범위 |
| --- | --- | --- |
| Controller·로컬 패널 | 키/행동 기반 노드, 조합, 연속 탭·누르기·떼기·홀드·대기, 최소·기본·최대값, 프리셋 JSON, JSON/YAML 시나리오, 보고서와 Replay | 모의 매핑·상태. Chrome 프리셋 왕복, YAML 실행과 Replay 비교를 최종 ZIP으로 검증 |
| HTTP·입력 안전 | bootstrap/session 토큰 분리, 단일 입력 소유권, 중복 ID 방지, 매핑·관측·소유권 revision 차단, heartbeat와 lease | 모의 런타임 1,000명령. Controller 프로세스 강제 종료 후 750ms lease 만료로 키 해제 및 새 세션 발급 |
| Decisions | Choice 검증, 후보 필터, 반복 탐색, 제한적 재시도, 호출·시간·토큰·예상 비용 제한 | 이전 실호출 1회에서 `JUMP` 선택, 입력 토큰 142개, 모의 점프 1회와 잔류 키 0 확인. [검증 기록](docs/mock-gate-status.md) |
| 프로젝트 준비 | 프로젝트 탐색·진단, 변경 미리보기와 승인, 설치·업데이트·제거·복구, 소스 fingerprint | 임시 프로젝트와 모의 플러그인 파일. 실제 UE 플러그인이나 Cook/Package는 미검증 |
| Windows 모의 배포 | PyInstaller onedir ZIP, Python 런타임 포함, 로컬 키 등록 도구 | 최종 ZIP을 풀고 Python이 없는 PATH에서 실행. Chrome 프로젝트 준비·프리셋, YAML·Replay 확인 |

기준선은 로컬 회귀 23건과 오프라인 Choice fixture 6건이다. [검증 근거와 ZIP 해시](docs/mock-gate-status.md)에 기록되어 있다. `UnrealAutoQA-Mock-win-x64.zip`은 모의 배포물이며 최종 제품 바이너리 `UnrealAutoQA.exe`는 남은 산출물이다. `qa.py`의 `transport()`는 현재 자체 모의 서버를 띄우며, **실제 UE 게임에 붙는 연결·실행 경로는 구현되어 있지 않다.**

### 소스에서 바로 실행

```powershell
# 소스 실행은 Python 3.10 이상. YAML 사용 시 아래 의존성이 필요하다.
python -m pip install PyYAML==6.0.3
python qa.py list-actions
python qa.py test --key SpaceBar --hold-ms 100 --expect IA_Jump
python qa.py run-scenario examples/movement-smoke.yaml
python qa.py explore --allow MOVE --allow JUMP --max-calls 2 --max-seconds 5
# 앞선 실행이 출력한 JSON 경로로 교체한다.
python qa.py replay 'runs/<run_id>.json'
```

패널은 별도 터미널에서 `python qa.py panel --open-browser` 또는 `Start-QA.cmd`로 시작한다. 기본 주소는 `http://127.0.0.1:18500`이다. `python --version`이 Python 버전을 출력하지 않으면 실제 Python 실행 파일을 지정한다. 모의 ZIP 재빌드는 `./build-mock.ps1 -Python '<실제 python.exe 경로>'`를 사용하며 도구는 `.qa-build/tools/`에 설치된다.

위 명령은 `--live`를 사용하지 않는다. 소스 보고서는 `runs/`에, 배포 실행 파일 보고서는 `%LOCALAPPDATA%/UnrealAutoQA/runs/`에 기록된다. 모의 탐색이 정해진 예산에서 정상 중단되면 `INCONCLUSIVE`와 종료 코드 1을 반환할 수 있다. 스크립트·수동 테스트 PASS는 0이다.

| 파일 | 이어서 볼 부분 |
| --- | --- |
| [qa.py](qa.py) | `loopback_client()`, `transport()`, `run_command()`, `execute_input()`, `explore()`, `run_scenario()`, `replay()`. 실제 UE 전송을 연결할 진입점 |
| [qa_mock.py](qa_mock.py), [명령 스키마](schemas/qa-command-v1.json) | 현재 정수 `schema_version: 1`의 HTTP 메시지, 상태, 세션·소유권·lease 동작. C++ 계약 테스트 기준 |
| [qa_nodes.py](qa_nodes.py), [qa_catalog.py](qa_catalog.py), [qa_scenario.py](qa_scenario.py), [qa_config.py](qa_config.py) | 입력 단계 정규화, 행동 승인 및 매핑 필터, 시나리오와 판정, 제한된 JSON/YAML 로더 |
| [qa_panel.html](qa_panel.html), [qa_project.py](qa_project.py), [qa_credentials.py](qa_credentials.py) | 프로젝트 준비·수동 입력 UI, 프로젝트 진단·fingerprint·설치 트랜잭션, DPAPI 키 등록 |
| [test_qa.py](test_qa.py), [브라우저 테스트](spikes/check_presets.cjs), [Decisions 테스트](spikes/decisions_smoke.py) | 로컬 회귀·Chrome·오프라인 API 응답 검증 |
| [샘플 시나리오](examples/movement-smoke.yaml), [샘플 Action Catalog](examples/action-catalog.yaml), [README](README.md) | 현재 모의 기능의 실행·설정 예시 |

소스 인수인계에는 이 PRD와 소스·`schemas/`·`examples/`·`spikes/`·`docs/`를 포함한다. `.qa-build/`, 캐시, 로컬 자격 증명은 전달할 필요가 없다. `runs/`는 필요한 검증 로그만 선별하고, 실호출 보고서를 같이 전달하지 않으면 표의 수치를 검증 기록으로 사용한다.

## 4. G0–G1 실제 입력 PoC

1. UE 샘플에서 활성 IMC, 대상 LocalPlayer·PlayerController·Pawn, 샘플의 전진·점프 매핑을 확인한다. `W`와 `SpaceBar`는 기본 샘플의 후보이며, 해당 빌드의 실제 키로 실험한다.
2. 5.9.0 설치본에서 게임 내부 FKey Down/Up을 전달할 후보를 컴파일한다. `UGameViewportClient::InputKey` 및 `FInputKeyEventArgs`, Enhanced Input의 `Input.+key`/`Input.-key`를 비교한다. 콘솔 경로를 실험하더라도 입력 API에 임의 콘솔 문자열을 받는 인터페이스를 열지 않는다. `InjectInputForAction`은 선택적 진단 경로이고 FKey/IMC 검증 증거와 구분한다.
3. **L1** 키 이벤트와 실제 게임 틱, **L2** Enhanced Input Action 이벤트, **L3** Pawn 위치·속도·지면 상태 변화를 각각 기록한다. API 반환 성공이나 L1만으로 게임 행동을 통과 처리하지 않는다.
4. 같은 Development 빌드에서 활성 창 전진·점프, 다른 앱에 포커스가 있는 상태의 전진·점프·키 조합, 홀드 중단 후 키 해제를 검증한다. 브라우저/VS Code 타이핑을 병행하며 사용자 앱에 QA 키가 들어가지 않고 포커스가 유지되는지 기록한다. 최소화는 게임 틱·입력·캡처를 따로 측정해 지원 여부를 표시한다.
5. UAT 로그, 빌드 ID와 실행 파일, 사용한 입력 경로, IN-01~04 결과와 L1/L2/L3 로그를 남긴다. **비활성 창에서 실제 이동·점프 성공 및 사용자 입력 침입·포커스 탈취 0회**가 G1 합격 조건이다.

## 5. G2–G6 실제 브리지와 QA 계약

### G2. 전송·세션·Watchdog

- 모의 계약에 맞춰 `/qa/v1/health`, `/capabilities`, `/session`, `/session/heartbeat`, `/session/owner`, `/session/stop`, `/mappings`, `/commands`, `/commands/{request_id}`, `/observations`, `/events?after_seq=N`, `/input/release-all`을 구현한다. 첫 경로에 붙은 `/qa/v1`은 전체에 공통이다. 메서드와 JSON 형태는 [모의 핸들러](qa_mock.py) 및 [스키마](schemas/qa-command-v1.json)를 대조한다.
- 실행 시 임시 루프백 포트와 bootstrap secret을 생성하고 게임에 로그가 남지 않는 채널로 전달한다. 세션 발급 이후 세션 토큰으로 요청을 인증한다. 브라우저 패널은 별도 토큰 및 Host/Origin 검증을 유지한다. 요청 크기·큐·입력 단계·홀드 시간에 상한을 둔다.
- `request_id` 동일·payload 동일 재전송은 상태만 반환하고, 내용이 다르면 `IDEMPOTENCY_CONFLICT`를 반환한다. 응답이 끊기면 상태를 확인한 후에만 재전송한다. 세션·월드·매핑·관측·소유권이 오래된 명령은 폐기한다. 서로 다른 프로세스의 단조 시계 값을 직접 비교하지 않는다.
- UObject와 실제 입력 처리는 Game Thread에서 짧게 실행한다. HTTP·직렬화·모델 호출·파일 기록은 Game Thread를 기다리게 하지 않는다. `SCRIPTED / DECISIONS / MANUAL` 중 한 소유자만 입력한다. 전환 순서는 신규 입력 차단 → 큐 취소 → `ReleaseAll` → 소유권 갱신이다.
- 월드 전환·중단·종료·단절·lease 만료에서 키를 모두 해제한다. 월드가 준비되기 전 새 명령은 받지 않는다. 승인된 키 목록, 최소 게임 틱과 최대 유지 시간, 동시 키 처리, 중복 Down/Up 정책을 브리지에서 검증한다.
- **합격:** 실제 패키징 게임에서 1,000명령, 중복 실행 0회, 오래된 명령 거부, 강제 종료 중 홀드 2초 이내 해제, 재접속 시 구 세션 무효화. 실제 실행·취소·회복 로그를 남긴다.

### G3–G4. 매핑 조회, 시나리오, 판정

- LocalPlayer의 활성 IMC, 우선순위, 실제 적용된 리매핑, Action 타입·키·Trigger·Modifier, UI 상태를 읽어 스냅샷을 만든다. 월드·Pawn·플레이어·IMC·리매핑 전환을 기록하고 유효 매핑이 달라지면 `mapping_revision`을 갱신한다. 축 방향·마우스·Chord는 샘플별로 실제 동작을 확인한다.
- **매핑을 먼저 읽고 시나리오를 작성한다.** `논리 행동 ID → 발견한 UE Input Action → 현재 유효 FKey → 실제 입력`을 연결한다. `selected_actions`는 실행 시 매핑을 조회하고, `selected_keys`는 사용자 지정 키만 사용한다. `all_mapped`에서도 승인·안전 조건을 적용한다. 미발견·모호한 매핑은 사유를 표시한다. 복수 후보를 선택할 경우 명시된 우선순위와 선택 근거를 기록한다.
- 개발자가 시나리오의 행동, 전제조건, 입력 단계, 기대 결과를 등록한다. 노드에는 탭·반복 탭·Down·Up·Hold·조합·Wait를 담고, 숫자 설정은 `{min, default, max, value?}`를 사용한다. 서버가 범위를 검사하고 사용한 값과 실제 FKey를 기록한다. 게임의 정상 조건을 확인하기 전에는 임의 행동을 AI 후보에 올리지 않는다.
- 기본 상태 어댑터는 Pawn transform·velocity·grounded·레벨·일시정지 등 공통 값을 읽는다. HP·퀘스트·인벤토리 등은 실제 게임 어댑터가 제공할 때만 사용한다. 미관측값은 unknown/null이다. 증분 이벤트는 `after_seq`와 제한된 버퍼를 사용하고 유실을 명시한다.
- **합격:** 다른 활성 IMC/리매핑에서 변경된 실제 키를 사용하고 이전 revision을 거부한다. 직접 입력한 단일 키와 조합을 패널에서 미리 보고 실행·취소한다. 의도적 이동·점프 결함은 `FAIL`, 관측 누락은 `INCONCLUSIVE`, 환경·도구·크래시 오류는 `ERROR`, 미지원은 `SKIPPED_UNSUPPORTED`, 중단은 `CANCELLED`로 분리한다. 필수 판정이 모두 충족되면 `PASS`다.

### G5. Decisions 실게임 폐루프

- Python Controller에서 `POST /v1/decisions`의 `gpt-6-luna` Choice를 호출한다. 현재 관측에서 필요한 상태만 전송하고, 개발자 승인 ∩ 활성 매핑 ∩ 실행 가능 ∩ 안전 허용 범위의 논리 행동과 `WAIT`를 후보로 제공한다. 빈 후보 집합이면 호출을 생략한다. 모델은 행동 ID를 선택하며 Controller와 UE가 실행·판정을 담당한다. [Decisions 가이드](https://developers.openai.com/api/docs/guides/decisions), [API 요청·응답 계약](https://developers.openai.com/api/reference/resources/decisions/methods/create)
- 정해진 이름의 Choice 답변이 정확히 하나이며 후보에 포함되는지 검사한다. refusal·누락·중복·범위 외 선택·타임아웃·오래된 응답에서는 게임 입력을 보내지 않고 안전하게 중단하거나 대기한다. 모델 응답 후 세션·월드·매핑·관측·입력 소유권·마감 시간을 다시 검사한다.
- 실제 게임으로 옮길 때 관측 수집과 모델 호출을 비동기 경계에 두고 동시 모델 요청을 1건으로 제한한다. 현재 요청 수·시간·토큰·예상 비용 제한과 일시 오류 재시도를 유지한다. 프로젝트 차원의 실제 청구 상한과 사용량 권한은 별도로 확인한다.
- **합격:** 실제 QA 게임에서 10분 자율 플레이. 후보 외 실행과 중단·타임아웃 후 잔류 입력 0회. 지연 P50/P95/P99, 무효·늦은 응답, 사용량을 기록한다. 정상·오류·재시도 정책은 먼저 오프라인 응답으로 재현한다.

### G6. 실제 보고서·크래시·Replay

- 실행별 실제 Engine/Cooked build hash, 플러그인·Controller 버전, 시나리오·프로필, 맵/월드/플레이어/seed, 요청·선택·타이밍, 매핑과 선택 근거, 사전·사후 관측, 판정, 관련 로그·크래시·측정 지표를 기록한다. 첫 실패 단계와 직전 입력이 보고서에서 바로 보여야 한다.
- 타임라인을 진행 중에 남겨 게임이 종료되거나 크래시해도 Controller가 종료 코드와 마지막 증거를 수집할 수 있게 한다. 키·토큰·계정 데이터는 리포트에서 제외한다.
- **합격:** 같은 빌드·맵·seed·초기 상태에서 기록된 입력으로 Replay하고 원본과 비교한다. 모델을 다시 호출하지 않는다. 빌드·매핑 불일치나 재현 차이를 표시하고, 알려진 정상과 의도적 결함을 구분한다.

## 6. G7–G8 최종 앱, 빌드와 출시 검증

- **G7 프로젝트 온보딩:** 기존 프로젝트 준비 UI와 설치 트랜잭션을 실제 UE 플러그인에 연결한다. 기본 스모크 흐름은 프로젝트 선택 → 진단 → 변경 미리보기/승인 → Development 빌드 → 연결·매핑 발견 → QA 시작 → 결과 보기다. Blueprint-only 프로젝트는 C++ 빌드 전제조건을 진단하고, 프로젝트 구성 변경은 별도 승인 후 진행한다.
- **G7 빌드 승격:** 프로젝트·플러그인·엔진·콘텐츠 fingerprint를 빌드 시작/종료 시 비교한다. 빌드는 독립 출력 및 QA 저장 위치에서 실행한다. 입력 변경은 `BUILD_STALE` 또는 `BUILD_INPUT_CHANGED_DURING_BUILD`로 표시한다. UAT 성공·실행 파일 생성·브리지 연결·입력 스모크를 모두 확인한 빌드만 마지막 정상 빌드로 승격한다. 실패·취소 시 원본과 이전 정상 빌드를 보존한다.
- **G7 배포 합격:** Python을 별도로 설치하지 않은 Windows에서 제품 `UnrealAutoQA.exe` onedir 배포판과 README만으로 기본 QA를 실행한다. 실제 플러그인 신규 설치·업데이트·제거·강제 종료 복구, 수정된 파일 보존, 빌드 중 파일 변경, Shipping 차단과 비밀값 미포함을 검증한다.
- **G8 이식·부하 합격:** 두 번째 UE 프로젝트에서 공통 Controller·플러그인 코드 수정 없이 프로젝트 설정과 어댑터로 이식한다. 5분 워밍업 후 사용자 작업과 30분 병행하고, QA ON/OFF의 작업 지연·빌드 시간·CPU/GPU/RAM 및 게임 프레임 타임을 비교한다. 사용자 입력 침입, 포커스 탈취, 잔류 키는 각 0회다. 스케줄러의 Game Thread 추가 소요는 P95 1ms 이하 목표다. 기능 QA는 30FPS/1280×720에서 시작할 수 있으며, 성능 회귀는 목표 해상도/FPS를 유지한다. 과부하는 관측·렌더 축소 또는 안전 일시정지를 검증한다.

| 출시 증거 | 커버해야 할 기존 수용 ID |
| --- | --- |
| 사용자 무간섭, 실제 IMC, 수동 키 지정 | AT-01, AT-02, AT-02B · CT-04~09 |
| Decisions 검증, 장애 내성, 상태 판정 | AT-03, AT-04 · CT-01~03, CT-10, CT-17~19 |
| 이식, 보고서·Replay, 릴리스·온보딩 | AT-05~08 · CT-11~16, CT-20 |

각 게이트마다 변경 diff, 실행 명령, 빌드/테스트 로그, 미검증 항목과 재시도 결과를 남긴다. **P0 실제 출시 합격은 G0–G8과 위 수용 항목을 실제 환경에서 충족했을 때 선언한다.** Gauntlet, Runtime MCP, 백그라운드 화면·UMG 수집, 이미지 기반 Decisions, 다중 인스턴스·멀티플레이, 코드 변경 제안은 P0 검증 이후의 선택 범위다.

