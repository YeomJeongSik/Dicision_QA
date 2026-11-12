# Unreal Auto QA

UE 5.9.0 Win64 Development 게임의 활성 Enhanced Input 매핑을 읽고 OpenAI Decisions로 다음 테스트 액션을 선택합니다.

## 시작하기

[소스 ZIP](https://github.com/YeomJeongSik/Dicision_QA/archive/refs/heads/main.zip)을 풉니다. 풀린 폴더의 이름을 `Dicision_QA`로 바꾸고 Unreal 프로젝트의 `.uproject` 바로 옆에 넣습니다.

```text
MyGame/
  MyGame.uproject
  Dicision_QA/
    Register-OpenAI-Key.cmd
    Project-QA.ps1
```

1. `Dicision_QA/Register-OpenAI-Key.cmd`를 한 번 실행하고 Decisions를 사용할 수 있는 OpenAI API 키를 입력합니다.
2. Codex처럼 터미널을 사용할 수 있는 AI에서 프로젝트 폴더를 열고 “Dicision_QA로 이 프로젝트에서 이동과 점프를 각각 확인해줘”라고 요청합니다. 동시 입력을 확인하려면 “이동하면서 점프할 수 있는지 확인해줘”라고 요청합니다.

AI가 도구 폴더의 [AGENTS.md](AGENTS.md)를 읽고 현재 프로젝트와 엔진을 찾습니다. 처음 실행하면 프로젝트 사본에 QA 브리지를 추가해 빌드합니다. 이후에는 변경되지 않은 QA 빌드와 첫 실행 때 기록한 매핑을 재사용하고, 실제 테스트 직전에 활성 매핑을 다시 확인합니다. 빌드 사본, QA 패키지, 실행 로그, 보고서와 암호화된 키는 `Dicision_QA/runs/`에 저장합니다.

QA 브리지는 활성 Enhanced Input에서 실행할 수 있는 키보드 매핑을 조회합니다. 한 액션에 여러 키가 있으면 AI가 키별 후보와 수정자를 볼 수 있습니다. Decisions가 요청한 항목 중 다음에 실행할 항목을 고릅니다. 한 번에 최대 12개 키를 입력할 수 있습니다.

일반 이동과 점프는 캐릭터의 움직임까지 검증합니다. `PASS`는 요청한 항목의 성공 조건을 모두 확인했다는 뜻입니다. 특정 방향 이동이나 상호작용에서 입력 발동까지만 확인하면 `INPUT_CONFIRMED`로 표시합니다. 실행하지 못한 항목은 `UNVERIFIED`로 남습니다.

지원 환경은 Windows x64와 UE 5.9.0 C++ 또는 Blueprint 프로젝트입니다. Python은 Unreal 설치본을 사용합니다. MSVC나 Windows SDK가 없으면 도구가 Microsoft 설치 프로그램을 실행합니다. Windows에서 관리자 승인 또는 재시작을 요청할 수 있습니다.

## 검증 범위

- 실제 C++ 및 Blueprint 프로젝트에서 QA 패키징과 Unreal의 키 매핑 조회를 확인했습니다. C++ 프로젝트에서 19개 매핑을 조회하고 12개 동시 입력의 발동과 해제를 확인했습니다.
- Blueprint 테스트는 기존 빌드를 재사용해 이동과 점프 동시 입력을 통과했습니다. 해당 실행에서 실제 Decisions 호출은 1회, 입력 사용량은 170토큰이었습니다.
- Microsoft 설치 파일의 디지털 서명과 설치 후 재개 분기를 모의 검증했습니다. 빌드 도구가 없는 실제 Windows 환경에서의 최초 설치는 미검증입니다.

## Decisions를 사용하는 이유

Unreal이 현재 매핑과 Pawn 상태를 제공합니다. Controller는 브리지가 실행할 수 있는 모호하지 않은 액션을 추려 액션 ID와 현재 키를 Choices로 전달합니다. OpenAI의 Decisions Choice는 앱이 제공한 값 중 하나를 선택하므로 매번 달라지는 실행 후보를 API 계약으로 제한할 수 있습니다. `WAIT`를 선택하면 입력 없이 다음 관측으로 넘어갑니다. [OpenAI Decisions 공식 가이드](https://developers.openai.com/api/docs/guides/decisions#select-from-fixed-options)

Controller는 응답의 후보 포함 여부와 현재 상태를 다시 검증한 후 실제 키를 전달합니다. 성공 판정은 UE의 Action 이벤트와 Pawn 움직임을 사용합니다. 개발자가 별도 키 시퀀스, 프리셋, 액션 카탈로그를 입력할 필요가 없습니다. 지원 범위는 브리지가 현재 실행할 수 있는 디지털 액션입니다. [API 요청·응답 계약](https://developers.openai.com/api/reference/resources/decisions/methods/create)

Decisions에는 실행 후보와 필요한 상태만 전송합니다. 같은 매핑에서 이미 판정한 액션은 다시 보내지 않습니다. `WAIT` 이후 후보와 전제조건이 같으면 호출을 종료하며 API 오류는 제품 테스트에서 자동 재시도하지 않습니다. 보고서에는 호출 전 추정 토큰과 API가 반환한 사용량이 따로 기록됩니다.

[검증 기록](docs/g2-bridge.md) · [개발 PRD](PRD.md)
