# UE 5.9.0 호환성 확인

2026-11-12 기준 G0 Win64 Development 기본 패키징과 G1 입력 검증을 통과했다. 실제 사용자 타이핑 중 QA 키 유입과 포커스 탈취는 관찰되지 않았다. [PoC 실행 결과](../spikes/background_input/README.md#2026-11-12-실행-결과)

| 항목 | 결과 | 확인 근거 |
| --- | --- | --- |
| Windows | 확인 | OS 빌드 `10.0.26200.0` |
| MSVC x64 | 발견 | Visual Studio Community 18, 도구 버전 `14.51.36231`, `bin/Hostx64/x64/cl.exe` 확인 |
| Windows SDK x64 | 발견 | `10.0.26100.0`, `Windows.h`와 x64 `kernel32.lib` 확인 |
| UE 5.9.0 및 UBT/UAT | 확인 | `C:/Program Files/Epic Games/UE_5.9`, CL `60351404`, 진단 종료 코드 0 |
| Development 패키징 | PASS | 별도 Third Person C++ 샘플, UAT 종료 코드 0, 패키징 EXE 확인 |
| Controller 오프라인 회귀 | PASS | 엔진 번들 Python으로 23건 실행 |
| Decisions Choice 오프라인 검증 | PASS | 정상, 답 누락, 중복, 허용 범위 밖 선택, 잘못된 값 타입, refusal 등 6건 |
| Decisions Choice 실호출 | PASS | 환경 조사 후 앱의 인증 경로로 실호출 1회, JUMP 선택·입력 토큰 142개·모의 점프 확인. [검증 기록](mock-gate-status.md) |

임시 파일로 명시적 엔진·프로젝트 경로 인식과 5.9.1의 버전 불일치 처리를 확인했다.

## 개발자용 실행

```powershell
./spikes/check_environment.ps1 -RequireReady
./spikes/check_environment.ps1 -EngineRoot 'D:/UE_5.9' -Project 'D:/QaSample/QaSample.uproject' -RequireReady
python spikes/decisions_smoke.py --self-test
```

환경 진단은 파일을 읽어 JSON을 출력하며 `-RequireReady`에서 누락 항목이 있으면 종료 코드 2를 반환한다. 성공은 파일 발견 기준이다. Choice 계약은 키 없이 오프라인으로 다시 검증할 수 있다. 실호출이 별도로 승인된 경우에는 등록된 키를 사용하는 앱의 인증 경로로 실행한다. [OpenAI Decisions 문서](https://developers.openai.com/api/docs/guides/decisions)

## UE 소스 대조 항목

5.9.0 설치본 헤더와 구현에서 아래 경로를 대조했다. 진단 JSON의 `engines[].files`에서 헤더 위치를 확인할 수 있다.

| 검증할 경로 | 실제로 확인할 내용 |
| --- | --- |
| `Engine`, `InputCore` | `UGameViewportClient::InputKey(const FInputKeyEventArgs&)`는 입력 장치로 LocalPlayer를 찾고 PlayerController에 전달한다. `CreateSimulated`로 FKey Down/Up 인자를 구성할 수 있다. 뷰포트 포커스 상실 처리에는 설정에 따른 키 플러시가 있으므로 비활성 상태에서 별도 검증한다. |
| `EnhancedInput` | 공개 `HasMappingContext`와 `GetEnhancedActionMappingsView`로 활성 IMC와 유효 매핑을 확인했다. `Input.+key`와 `Input.-key`는 디버그 키 강제 입력을 등록·해제한다. |
| `HTTPServer` | `FHttpServerModule::GetHttpRouter`, `IHttpRouter::BindRoute` 선언을 확인했다. 모듈 링크와 루프백 동작 검증은 G2에 남아 있다. |

## 기본 패키징 기록

설치본 `TP_ThirdPerson` 템플릿에 High 공용 에셋 세 묶음(`LevelPrototyping`, `Characters`, `Input`)을 포함해 작업 폴더 안에 별도 샘플을 준비했다. 아래 경로는 저장소 루트 기준이다.

- [환경 진단](../runs/g0-20261112-102147/environment.json), [BuildCookRun 도움말](../runs/g0-uat-help/Log.txt), [실행 인자](../runs/g0-20261112-102147/uat-args.txt), [UAT 성공 로그](../runs/g0-20261112-102147/uat-baseline-console.log).
- VS 2022 MSVC `14.44.35228`과 SDK `10.0.26100.0`을 사용했다. Cook 패키지 603개, UAT 종료 코드 0을 확인했다. 제한 실행에서 UAT 로그 및 UBA 캐시 접근 오류가 발생했고, 별도 로그 경로와 일반 권한 실행으로 완료했다.
- [기본 빌드 실행 파일](../runs/g0-20261112-102147/Package/Windows/TP_ThirdPerson.exe). 패키지 내부의 실제 Win64 게임 EXE SHA-256은 `099a451d62537032bb4b264d67513ca7e598cef2688428b01f8c2cf89afe6f3f`다.
- G1의 IN-01~04 게임 로그와 입력 경로 비교는 [PoC 실행 결과](../spikes/background_input/README.md#2026-11-12-실행-결과)에 기록했다.
