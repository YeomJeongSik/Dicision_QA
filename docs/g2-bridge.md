# 실제 UE 브리지 검증 기록

`GameQABridge`는 UE 5.9.0 Win64 Development 게임에서 실행한다. 패키지의 최상위 실행 파일 대신 `<프로젝트>/Binaries/Win64/<프로젝트>.exe`를 Controller에 지정한다.

```powershell
python -B qa.py bridge-health "<게임 EXE>"
python -B qa.py ai-test "<게임 EXE>" --max-calls 2
python -B qa.py panel --game "<게임 EXE>" --open-browser
```

배포 ZIP에서는 `python -B qa.py` 대신 `UnrealAutoQA.exe`를 실행한다. 샘플에 이미 적용된 브리지가 필요하다. 원본 프로젝트를 복제한 뒤 패키징하는 명령은 아래와 같다.

제품 테스트는 실시간 매핑으로 Decisions Choice 후보를 구성한다. 아래의 기존 수동 입력·Replay 기록은 개발 검증 증거다. [제품 사용 방법과 공식 문서 근거](../README.md)

실제 패키지의 자동 조회에서 `IA_Move`, `IA_Jump`, `WAIT`를 후보로 만들었다. 로컬 Decisions 응답 fixture가 `IA_Jump`를 반환하자 UE에서 점프 상승과 키 해제를 확인했다. 이 검증은 외부 호출 없이 실행했다. [자동 테스트 보고서](../runs/31c7ede0a2054afb875b8237e3d178be.json)

```powershell
./build-game.ps1 -Project "<C++ .uproject>" -EngineRoot "C:/Program Files/Epic Games/UE_5.9"
```

복제는 파일별 SHA-256을 확인한다. 소스가 포함된 플러그인의 빌드 출력 폴더는 복제 대상에서 제외하며, 바이너리 전용 플러그인의 런타임 파일은 포함한다.

## 수용 시험 현황

| 단계 | 실게임에서 확인한 범위 | 남은 범위 |
| --- | --- | --- |
| G2 | 요청 한도, 인증, 세션·소유권, heartbeat·lease, 겹친 디지털 입력의 서버 스케줄링. 1,000명령과 중복 제출 1,000건에서 키 이벤트 2,000개. Controller 강제 종료 후 W 해제 750ms 및 기존 세션 토큰 거부. | 월드 전환·일시정지·포커스 전환의 조합 시험 |
| G3 | 활성 IMC·우선순위, 유효 키·액션 타입·트리거·모디파이어 조회. 실제 W→I 리매핑에서 revision 1→2와 오래된 명령 거부, 새 키로 전진 확인. | 축·마우스·Chord와 게임별 상태 어댑터 검증 |
| G4 | 실제 패키지 시나리오 PASS, 점프를 비활성화한 결함 빌드 FAIL. 겹친 홀드의 전환 순서, 착지 후 점프 판정, 패널에서 홀드 중 취소와 키 해제를 검증했다. | 다른 게임별 판정·관측 누락 수용 범위 |
| G5 | 오프라인 Choice와 승인된 Decisions 실호출 1회로 실제 게임의 JUMP와 잔류 키 해제 확인. 실호출 경로에 heartbeat와 호출·시간·토큰·예상 비용 제한 적용. | 별도 한도 승인 후 10분 실호출 및 실제 청구·권한 검증 |
| G6 | 실제 EXE와 Cooked 파일 SHA-256, JSON/HTML 및 단계별 JSONL. 의도적 크래시에서 종료 코드·로그·덤프 수집, 동일 빌드 전체 시나리오 및 기록된 Decisions 입력 Replay 검증. | seed·장면의 완전한 동일성 검증 |
| G7 | 실제 플러그인 설치·제거, 업데이트 중 프로세스 종료 후 이전 버전 복구와 재업데이트, 격리 패키징·입력 스모크·마지막 정상 빌드 승격. Shipping 리스너 차단. | 전체 패널 온보딩 자동화, 입력 변경 중 빌드 수용 시험. 변경 시험에서는 UAT가 정해진 대기 시간 안에 끝나지 않아 승격 단계까지 검증하지 못했다. |
| G8 | 같은 브리지와 Controller로 두 번째 First Person C++ 프로젝트에서 전진·점프 성공. | 사용자 작업과 30분 병행, QA ON/OFF 부하·입력 간섭·프레임 지연 측정 |

## 전송과 입력

게임 프로세스가 IPv4 루프백의 임시 포트를 직접 바인딩하고 Controller에 stdout 파이프로 번호를 알린다. Bootstrap 값은 자식 프로세스 환경에만 전달되고 게임에서 읽은 직후 삭제한다. 세션 토큰은 별도로 발급한다. 전송은 UE 번들 Boost.Beast 워커에서 실행한다. HTTP 헤더는 4 KiB, 본문은 16 KiB, 동시 처리 연결은 1개, 소켓 읽기·쓰기는 단계별 2초로 제한한다. Game Thread는 키와 Pawn을 처리하고, 워커는 짧은 잠금으로 스냅샷을 읽은 뒤 잠금 밖에서 JSON을 직렬화한다. 게임이 종료되면 리스너도 닫힌다.

서버 명령은 승인된 디지털 키의 단일 홀드와 최대 96개 전환을 포함한 타임라인을 받는다. 타임라인은 겹친 홀드와 대기를 하나의 명령으로 실행한다. 전환 시각은 게임 틱 해상도에 따르며, 마감 시간을 넘긴 명령은 취소한다. 월드 틱 공백이 750ms 이상이면 heartbeat가 유효해도 Game Thread 복구 시 입력을 해제하고 남은 전환을 폐기한다. 점프 성공은 착지 후 최종 위치 대신 실행 중 `JumpRise` 관측으로 판정한다.

기본 승인 키는 W와 SpaceBar다. 프로젝트의 `DefaultGame.ini`에 `[GameQABridge]`와 `+ApprovedKeys=I`처럼 승인 키를 지정할 수 있다. 유효 매핑에 승인 키가 둘 이상이면 해당 액션을 실행 후보에서 제외한다.

## 증거

- G2 [UAT 빌드](../runs/g2-20261112-115215/uat-121154-console.log), [1,000명령·강제 종료 시험](../runs/g2-20261112-115215/g2-acceptance.log). 재현 명령은 `python -B -m spikes.bridge_session_smoke "<G2Package의 게임 EXE>" --commands 1000 --controller-death`다.
- 액션 빌드 [UAT](../runs/g2-20261112-115215/uat-121549-console.log), [동시 입력](../runs/5f9ccf2b823e4297b632e6be0702aef3.json), [Replay](../runs/28668dc79e2a483180ef1e651693f400.json), [기대 액션 불일치](../runs/02fdb93f88dc4318af644c49e2448d2c.json), [시나리오](../runs/c461a65b40fe40e99cc303395487e3fa.json).
- [오프라인 선택 실게임 보고서](../runs/82a2155d2f9d4ecb8179818910b68e12.json), [실제 패널](../runs/g2-20261112-115215/panel-ue-smoke.log), [두 번째 프로젝트](../runs/9436ea0d9fd84f76b8a88dd548c5344c.json).
- [Decisions 실호출 1회](../runs/d0f91cd8792548da90b6b78c68a274f4.json). 입력 174토큰, 응답까지 2.14초, `JUMP` 선택, `IA_Jump` 발동 및 Pawn 약 48cm 상승, 종료 후 잔류 키 0개를 확인했다. 예상 비용은 Controller 기준으로 $0.0000174였다. 전체 실행은 호출 예산 1회를 소진해 정상 종료했다.
- [W→I 리매핑 빌드](../runs/g2-20261112-115215/Remap/uat-i-console.log), [리매핑 실게임 검증](../runs/g2-20261112-115215/Remap/remap-i-smoke.log), [반복 탭·대기·Down/Up](../runs/ba04f3c34cdb434c9da79f0c21f16b19.json). 첫 리매핑 후보 Up은 기본 IMC에 이미 있어, W와 Up을 동시에 승인했을 때 이동 액션이 후보에서 빠지는 것도 확인했다.
- [패널 타임라인 미리보기·실행](../runs/g2-20261112-115215/Remap/node-panel-i-smoke.log), [패널 홀드 취소](../runs/g2-20261112-115215/Remap/panel-cancel-smoke.log).
- [입력 복구 검증](../runs/bridge-recovery/acceptance.log). 패키지 한 번의 실행에서 겹친 홀드의 키 순서, 착지한 점프의 판정, heartbeat가 유지된 1.1초 Game Thread 지연 뒤 키 해제를 확인했다. 재현 명령은 `python -B -m spikes.bridge_recovery_smoke "<지연을 주입하는 fixture 게임 EXE>"`다.
- [결함·크래시 검증](../runs/bridge-faults/fault-smoke.log), [시나리오 Replay](../runs/bridge-faults/scenario-replay.log), [Decisions 입력 Replay](../runs/bridge-faults/exploration-replay.log), [실제 플러그인 업그레이드 중단 복구](../runs/bridge-faults/plugin-recovery.log).
- [빌드 입력 변경 차단](../runs/bridge-faults/stale-guard.log). 실제 UAT가 대기 시간 안에 완료되지 않아, UAT를 대체한 fixture로 배포 스크립트 자체를 검증했다. 원본 변경을 감지해 입력 스모크 전에 중단했고 마지막 정상 빌드 기록의 해시는 유지됐다.
- [설치·제거](../runs/g2-20261112-115215/install-smoke.log), [빌드 승격 결과](../runs/g2-20261112-115215/build-promotion.json), [Shipping 차단](../runs/g2-20261112-115215/shipping-smoke.log), [Python 없는 PATH 실행](../runs/g2-20261112-115215/product-smoke.log).

## 사용자가 참여하는 검증

- G5의 1회 확인은 등록한 키로 완료했다. 10분 실호출에 필요한 최대 호출 수 또는 비용 한도는 별도 승인 대상이다.
- 실제 게임 프로젝트에 플러그인을 설치하거나 프로젝트 파일을 바꾸기 전에는 변경 미리보기와 대상 경로를 확인한다. 위 검증은 작업 폴더의 별도 fixture에서 실행했다.
- G8 최종 시험에서는 일반 작업과 QA를 30분 병행한다. 사용자가 평소 입력을 사용하는 동안 입력 유입과 포커스 탈취를 함께 확인한다.
