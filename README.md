# Unreal Auto QA

UE 5.9.0 Win64 Development 게임의 활성 Enhanced Input 매핑을 읽고 OpenAI Decisions로 다음 테스트 액션을 선택합니다.

## AI에게 테스트 요청하기

아래 준비를 마친 뒤 Codex처럼 터미널을 실행할 수 있는 AI에서 이 저장소 폴더를 엽니다. 게임 EXE 경로와 검증 목표를 알려주세요. 예를 들어 “이 게임에서 캐릭터가 지면에서 점프할 수 있는지 테스트해줘. 게임 EXE는 `C:\QA\MyGame\Binaries\Win64\MyGame.exe`야”라고 요청할 수 있습니다.

AI는 [AGENTS.md](AGENTS.md)의 안내에 따라 CLI에 게임 경로와 자연어 목표를 전달합니다. Unreal이 현재 입력 매핑과 상태를 제공하고, Decisions가 목표에 맞는 동작을 선택합니다. AI는 Unreal이 기록한 결과를 읽어 답합니다. 첫 요청의 기본 호출 예시는 다음과 같습니다.

```powershell
python -B qa.py ai-test "<게임 EXE>" --objective "지면에서 캐릭터가 점프할 수 있는지 확인" --check IA_Jump
```

이동과 점프를 각각 확인하려면 `--check IA_Move --check IA_Jump`, 동시에 확인하려면 `--check IA_Move+IA_Jump`를 사용합니다. AI가 동작 ID를 모를 때는 `list-actions "<게임 EXE>"`로 조회할 수 있습니다. 이 조회에는 Decisions 호출이 발생하지 않습니다.

동시 검증 한 항목에 활성 Enhanced Input 액션을 최대 12개까지 `+`로 연결할 수 있습니다. 각 액션은 서로 다른 승인 키 하나에 연결되어 있어야 합니다. 기본 승인 키는 `W`와 `SpaceBar`입니다. 더 많은 키를 테스트하려면 프로젝트의 `Config/DefaultGame.ini`에 다음처럼 승인 키를 등록하고 새 GameQABridge로 QA 패키지를 다시 빌드하세요.

```ini
[GameQABridge]
+ApprovedKeys=W
+ApprovedKeys=SpaceBar
+ApprovedKeys=E
```

같은 승인 키를 공유하는 액션이나 승인 키가 여러 개인 액션은 후보에서 제외됩니다. 조건에 맞는 매핑이 12개를 넘으면 Unreal의 이름 정렬 기준으로 처음 12개를 사용합니다. Development 빌드에서는 기본 디버그 단축키와 겹치지 않는 키를 승인하세요. 이동과 점프는 캐릭터의 움직임까지 확인하며, 그 외 액션은 Unreal의 Enhanced Input 트리거를 확인합니다.

기본 호출 상한은 요청한 항목 수입니다. 모든 항목의 판정이 끝나면 종료하며, 전체 `PASS`는 모든 항목이 통과했을 때만 반환합니다. 실행하지 못한 항목은 `UNVERIFIED`로 표시됩니다.

## 실행 준비와 브라우저 패널

Windows x64에서 [소스 ZIP](https://github.com/YeomJeongSik/Dicision_QA/archive/refs/heads/main.zip)을 내려받아 압축을 풉니다. ZIP에는 QA 도구의 소스와 GameQABridge 플러그인이 들어 있습니다. 테스트에 사용할 게임은 별도로 준비해야 합니다. AI 테스트에는 OpenAI Decisions에 접근할 수 있는 본인의 API 키가 필요합니다.

1. **게임 준비.** GameQABridge가 활성화된 Win64 Development 패키지가 있다면 패키지 내부의 `<프로젝트>/Binaries/Win64/<프로젝트>.exe` 경로를 사용하세요. 새로 빌드하려면 UE 5.9.0, Unreal C++ 빌드 도구, Enhanced Input을 사용하는 C++ 프로젝트가 필요합니다. 압축을 푼 폴더에서 PowerShell을 열고 아래 경로를 수정해 실행하세요. 스크립트는 프로젝트 사본에 플러그인을 활성화하고, 성공하면 출력 JSON의 `executable`에 실행할 EXE 경로를 표시합니다. 기본 맵이 설정되어 있지 않다면 `-Map "/Game/Maps/YourMap"`을 추가하세요.

```powershell
.\build-game.ps1 -Project "C:\Games\MyGame\MyGame.uproject" -EngineRoot "C:\Program Files\Epic Games\UE_5.9"
```

2. `Register-OpenAI-Key.cmd`를 실행하고 키를 등록합니다. 키는 현재 Windows 사용자 범위의 DPAPI로 암호화해 저장합니다. 실행 스크립트는 Python 3.10 이상을 사용하며 기본 경로에 설치된 UE 5.9의 Python도 사용할 수 있습니다.
3. 브라우저에서 직접 실행하려면 아래 명령에 1단계에서 확인한 게임 EXE의 전체 경로를 넣습니다. 브라우저가 자동으로 열리지 않으면 터미널에 표시된 로컬 패널 주소를 여세요.

```powershell
.\Start-QA.cmd --game "C:\QA\Package\Windows\MyGame\Binaries\Win64\MyGame.exe"
```

4. 브라우저 패널에서 **AI 테스트 시작**을 누릅니다. 첫 확인에는 최대 호출 수를 1로 설정하세요. Decisions가 Unreal에서 읽은 실행 가능 액션 중 하나를 선택하면, 패널에서 선택 결과와 Unreal 판정, 입력 토큰 사용량을 확인할 수 있습니다. 테스트 시작 시 유료 Decisions 호출이 발생할 수 있습니다.

GitHub ZIP을 새 폴더에 풀고 기존 UE 5.9.0 Development 패키지를 연결해 이 실행 경로를 검증했습니다. Decisions 호출 1회에서 `IA_Move`를 선택했고 Unreal에서 이동과 키 해제를 확인했습니다. 결과는 `PASS`, 입력 사용량은 171토큰이었습니다.

Windows 실행 파일 번들을 직접 만들려면 `.\build-mock.ps1 -Product -Python "<Python EXE 경로>"`를 실행합니다. 생성된 `UnrealAutoQA-win-x64.zip`을 풀고 `UnrealAutoQA.exe panel --game "<게임 EXE>" --open-browser`를 실행하면 같은 패널을 사용할 수 있습니다.

## Decisions를 사용하는 이유

Unreal이 현재 매핑과 Pawn 상태를 제공합니다. Controller는 브리지가 실행할 수 있는 모호하지 않은 액션을 추려 액션 ID와 현재 키를 Choices로 전달합니다. OpenAI의 Decisions Choice는 앱이 제공한 값 중 하나를 선택하므로 매번 달라지는 실행 후보를 API 계약으로 제한할 수 있습니다. `WAIT`를 선택하면 입력 없이 다음 관측으로 넘어갑니다. [OpenAI Decisions 공식 가이드](https://developers.openai.com/api/docs/guides/decisions#select-from-fixed-options)

Controller는 응답의 후보 포함 여부와 현재 상태를 다시 검증한 후 실제 키를 전달합니다. 성공 판정은 UE의 Action 이벤트와 Pawn 움직임을 사용합니다. 개발자가 별도 키 시퀀스, 프리셋, 액션 카탈로그를 입력할 필요가 없습니다. 지원 범위는 브리지가 현재 실행할 수 있는 디지털 액션입니다. [API 요청·응답 계약](https://developers.openai.com/api/reference/resources/decisions/methods/create)

Decisions에는 실행 후보와 필요한 상태만 전송합니다. 같은 매핑에서 이미 판정한 액션은 다시 보내지 않습니다. `WAIT` 이후 후보와 전제조건이 같으면 호출을 종료하며 API 오류는 제품 테스트에서 자동 재시도하지 않습니다. 보고서에는 호출 전 추정 토큰과 API가 반환한 사용량이 따로 기록됩니다.

[검증 기록](docs/g2-bridge.md) · [개발 PRD](PRD.md)
