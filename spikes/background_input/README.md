# G0–G1 백그라운드 입력 검증

UE 5.9.0 Win64 Development용 Third Person C++ 샘플에서 실행하는 일회성 PoC다. `-QAInputProbe=`에 `move`, `jump`, `combo`, `cancel` 중 하나를 지정하면 로컬 플레이어와 Pawn이 준비된 뒤 10초 후 시작한다. 한 실행에서 한 케이스를 측정한다. 기본 입력 경로는 `UGameViewportClient::InputKey`다. `-QAForcedKey`는 고정된 `Input.+key` / `Input.-key` 경로를 비교한다.

## 샘플 적용 기록

기본 Development 패키징 성공과 사용자 승인 후 복제 샘플에 적용했다.

- 기준 샘플은 `runs/g0-20261112-102147/Sample/TP_ThirdPerson.uproject`다. 설치본 템플릿과 High 공용 에셋을 복제했다.
- `BackgroundInputProbe/`의 파일 세 개를 샘플의 `Plugins/BackgroundInputProbe/`에 추가한다.
- 샘플의 `.uproject`에 `{"Name":"BackgroundInputProbe","Enabled":true}`를 `Plugins` 배열 항목으로 추가한다. 적용 전 원본은 `runs/g0-20261112-102147/TP_ThirdPerson.uproject.before-probe`에 백업한다.
- 기존 패키지를 보존하고 동일 UAT 인자에서 `-archivedirectory`와 로그 경로를 별도의 PoC 출력으로 바꾼다.

| 파일 | 적용 전 확인할 SHA-256 |
| --- | --- |
| 샘플 `TP_ThirdPerson.uproject` | `8bf5a89f65c7cb115a999b9af5b10019a7fc7a69b4223e3223f74b7d57eea694` |
| `BackgroundInputProbe.uplugin` | `5d1719c1547e0804c660606acd82e8499a7c67feaf8aa647ab53d0048a6b59ce` |
| `Source/BackgroundInputProbe/BackgroundInputProbe.Build.cs` | `e5a951fcbe6ab9e23de3c30e9c356057cdc3544668ad3ebf1cc71c200570d1e3` |
| `Source/BackgroundInputProbe/Private/BackgroundInputProbe.cpp` | `1ae3afd09fc0fbdce36e7de073665d0132795a63594350f0dbdc59096399d679` |

첫 컴파일에서 `GetAppliedInputContextData`가 보호된 멤버임을 확인했다. 설치본의 공개 `HasMappingContext(Context, Priority)`로 교체한 뒤 [Win64 Development 패키징](../../runs/g0-20261112-102147/uat-probe-103033-console.log)에 성공했다. 현재 CPP SHA-256은 `ace27e8cafc052fc2fc6e765aaae8cdafd8786642a2a73210e689fd7ec76f9dc`다.

PoC 빌드의 실제 게임 EXE SHA-256은 `87a95a9ac9982548f44df399c354ceb7815f0f03bd69efed02988798f5144a30`이다. [실행 파일](../../runs/g0-20261112-102147/ProbePackage/Windows/TP_ThirdPerson.exe)과 [UAT 인자](../../runs/g0-20261112-102147/uat-probe-args.txt)를 보관했다.

## 검증 절차

예시 실행 인자는 `-windowed -ResX=1280 -ResY=720 -QAInputProbe=combo -abslog=<절대경로>`다. 실행마다 로그 파일을 다르게 지정한다. 활성 케이스에서 게임에 물리 키를 입력하지 않는다.

| ID | 실행 | 확인 항목 |
| --- | --- | --- |
| IN-01 | 게임 창 활성 상태에서 `move`와 `jump` 각각 실행 | `IA_Move`와 `IA_Jump` 트리거, 수평 이동과 상승 |
| IN-02 | 10초 대기 동안 브라우저 또는 VS Code로 전환한 뒤 `move`와 `jump` 각각 실행 | 비활성 창에서 IN-01과 동일한 관측, 타이핑 내용과 포커스 유지 |
| IN-03 | 다른 앱에 포커스를 둔 상태에서 `combo` 실행 | 같은 실행에서 전진과 점프, 사용자 앱으로 전달된 QA 키 0건 |
| IN-04 | 다른 앱에 포커스를 둔 상태에서 `cancel` 실행 | 800ms 홀드를 200ms에 중단하고 Up 및 이후 키 상태 해제를 확인 |

각 케이스에서 `L1`의 Down/Up 및 프레임 번호, `L2`의 Action 트리거와 키 상태, `L3`의 위치·속도·지면 상태를 확인한다. L2 트리거의 정수 `1`은 `Triggered`다. `Probe result`는 게임 내부 관측만 판정한다. 포커스와 사용자 앱의 입력 내용은 같은 시간대의 별도 관찰로 기록한다.

최소화는 별도 실행에서 게임 틱과 L1–L3 로그를 확인한다. 캡처 여부는 실제 캡처 관측으로 기록한다. 두 입력 경로를 비교할 때에는 같은 패키지에서 케이스별로 한 번 더 실행한다.

## 2026-11-12 실행 결과

모든 실행에서 `IMC_Default`와 `IMC_MouseLook`이 활성 상태였고 우선순위는 각각 0이었다. 샘플에서 유효한 `W → IA_Move`, `SpaceBar → IA_Jump`를 사용했다.

| ID | 게임 관측 | 로그 |
| --- | --- | --- |
| IN-01 | PASS. 활성 창에서 각각 전진 및 상승, Action 트리거, 키 해제 확인 | [전진](../../runs/g0-20261112-102147/probe-runs/active-move-viewport.log), [점프](../../runs/g0-20261112-102147/probe-runs/windowed-jump-viewport.log) |
| IN-02 | PASS. 비활성·비최소화 창에서 각각 전진 및 점프. 타이핑 병행 관측은 IN-03의 두 키 동시 실행에서 확인 | [전진](../../runs/g0-20261112-102147/probe-runs/background-move-viewport-103441.log), [점프](../../runs/g0-20261112-102147/probe-runs/background-jump-viewport-103456.log) |
| IN-03 | PASS. 비활성 창의 동시 입력으로 전진·점프·키 해제. 실제 타이핑에서 QA 키 유입과 포커스 탈취 0회 관찰 | [재시험 게임 로그](../../runs/g0-20261112-102147/probe-runs/manual-retry-combo-viewport.log), [전경 창 관측](../../runs/g0-20261112-102147/probe-runs/manual-retry-combo-viewport.log.foreground) |
| IN-04 | PASS. 비활성·비최소화 창에서 홀드를 중단하고 키 해제를 확인 | [중단](../../runs/g0-20261112-102147/probe-runs/background-cancel-viewport-103524.log) |

비활성 시험에서 외부 관측 스크립트가 입력 시작 전에 샘플 창을 최소화한 뒤 활성화 없이 복원했다. Win32 전경 창 핸들은 준비 완료부터 결과까지 같은 다른 창에 유지됐다. 샘플은 같은 구간의 모든 L3 틱에서 `foreground=0 minimized=0`을 기록했다. [동시 입력의 전경 창 관측](../../runs/g0-20261112-102147/probe-runs/background-combo-viewport-103510.log.foreground)

두 경로 모두 동일 빌드의 비활성 창에서 동시 입력으로 전진·점프와 키 해제를 기록했다. G2의 FKey 입력 경로는 `UGameViewportClient::InputKey`로 확정했다. `Input.+key` / `Input.-key`는 디버그 비교에 사용했다. [Enhanced Input 비교 로그](../../runs/g0-20261112-102147/probe-runs/background-combo-forced-103552.log)

최소화 시험은 166개 L3 틱에서 `foreground=0 minimized=1`을 기록했고 전진·점프·키 해제가 통과했다. Win32 `PrintWindow`는 최소화 상태에서 실패를 반환했다. 같은 실행의 표시 상태에서도 유효 픽셀을 얻지 못했으므로, 이 캡처 경로는 검증되지 않았다. [최소화 게임 로그](../../runs/g0-20261112-102147/probe-runs/minimized-combo-viewport-103538.log), [캡처 관측](../../runs/g0-20261112-102147/probe-runs/minimized-combo-viewport-103627.log.capture)

### 실제 타이핑 병행 확인

첫 수동 실행에서는 입력 구간 전체에서 게임이 비활성 상태였고 동시 입력과 키 해제가 통과했다. 사용자가 타이핑을 제때 시작하지 못했다고 확인해 재시험했다. [첫 수동 실행 로그](../../runs/g0-20261112-102147/probe-runs/manual-combo-viewport.log)

11:48 KST 재시험에서 사용자는 숫자만 입력됐고 포커스도 유지됐다고 확인했다. 게임 로그의 288개 L3 틱은 모두 `foreground=0 minimized=0`이었고, 동시 입력과 키 해제는 `PASS residual=0`이었다. 외부 관측에서도 전경 창 핸들이 유지됐다. [재시험 게임 로그](../../runs/g0-20261112-102147/probe-runs/manual-retry-combo-viewport.log), [전경 창 관측](../../runs/g0-20261112-102147/probe-runs/manual-retry-combo-viewport.log.foreground)

작업 폴더의 PowerShell에서 아래 명령으로 동시 입력 케이스를 시작한다. 게임 창이 뜨면 10초 안에 브라우저나 VS Code의 빈 입력란으로 전환한다. 약 15초 동안 숫자만 직접 타이핑하고 중간에 `w`나 공백이 섞이는지, 게임이 포커스를 가져가는지 확인한다. 게임 창은 시험이 끝난 뒤 닫는다.

```powershell
$package = Join-Path $PWD 'runs/g0-20261112-102147/ProbePackage/Windows'
$log = Join-Path $PWD ('runs/g0-20261112-102147/probe-runs/manual-combo-' + (Get-Date -Format yyyyMMdd-HHmmss) + '.log')
& "$package/TP_ThirdPerson.exe" -windowed -ResX=1280 -ResY=720 -nosound -forcelogflush -QAInputProbe=combo "-abslog=$log"
```

로그의 `foreground=0 minimized=0`, `Probe result=PASS`와 입력란에서 관찰한 결과를 함께 기록한다.
