# Unreal Auto QA

UE 5.9.0 Win64 Development 게임의 활성 Enhanced Input 매핑을 읽고 OpenAI Decisions로 다음 테스트 액션을 선택합니다.

## 실행

1. GameQABridge가 활성화된 게임을 패키징합니다. 소스 또는 배포 폴더에서 `build-game.ps1`로 원본과 분리된 QA 빌드를 만들 수 있습니다.
2. 최초 한 번 `Register-OpenAI-Key.cmd`로 키를 등록합니다. 키는 Windows 사용자 범위 DPAPI에 저장됩니다.
3. 패키지 내부의 실제 게임 EXE를 지정하고 패널의 **AI 테스트 시작**을 누릅니다.

```powershell
./build-game.ps1 -Project "<프로젝트.uproject>" -EngineRoot "C:/Program Files/Epic Games/UE_5.9"
./UnrealAutoQA.exe panel --game "<Project/Binaries/Win64/Project.exe>" --open-browser
```

소스에서는 `Start-QA.cmd --game "<게임 EXE>"`를 사용할 수 있습니다. CLI에서는 `UnrealAutoQA.exe ai-test "<게임 EXE>" --max-calls 2`로 실행합니다. 테스트 시작은 등록된 키를 사용한 유료 Decisions 호출을 요청합니다. 패널의 비용 기준은 실행 중단에 사용하는 예상치입니다.

## Decisions를 사용하는 이유

Unreal이 현재 매핑과 Pawn 상태를 제공합니다. Controller는 브리지가 실행할 수 있는 모호하지 않은 액션을 추려 액션 ID와 현재 키를 Choices로 전달합니다. OpenAI의 Decisions Choice는 앱이 제공한 값 중 하나를 선택하므로 매번 달라지는 실행 후보를 API 계약으로 제한할 수 있습니다. `WAIT`를 선택하면 입력 없이 다음 관측으로 넘어갑니다. [OpenAI Decisions 공식 가이드](https://developers.openai.com/api/docs/guides/decisions#select-from-fixed-options)

Controller는 응답의 후보 포함 여부와 현재 상태를 다시 검증한 후 실제 키를 전달합니다. 성공 판정은 UE의 Action 이벤트와 Pawn 움직임을 사용합니다. 개발자가 별도 키 시퀀스, 프리셋, 액션 카탈로그를 입력할 필요가 없습니다. 지원 범위는 브리지가 현재 실행할 수 있는 디지털 액션입니다. [API 요청·응답 계약](https://developers.openai.com/api/reference/resources/decisions/methods/create)

Decisions에는 실행 후보와 필요한 상태만 전송합니다. 같은 매핑에서 이미 판정한 액션은 다시 보내지 않습니다. `WAIT` 이후 후보와 전제조건이 같으면 호출을 종료하며 API 오류는 제품 테스트에서 자동 재시도하지 않습니다. 보고서에는 호출 전 추정 토큰과 API가 반환한 사용량이 따로 기록됩니다.

[검증 기록](docs/g2-bridge.md) · [개발 PRD](PRD.md)
