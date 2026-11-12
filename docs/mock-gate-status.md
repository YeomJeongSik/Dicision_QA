# UE 없이 검증한 개발 범위

2026-11-11. 이 문서는 [인수인계 PRD](../PRD.md)의 제품 게이트와 모의 환경에서 확보한 증거를 함께 기록한다.

| Gate | 모의 환경에서 구현·검증한 범위 | 실제 게이트에 남은 작업 |
| --- | --- | --- |
| G0–G1 | 환경 진단, 기존 Decisions 실호출 보고서, 모의 KeyDown/Up·Action·상태 이벤트 | UE 5.9.0 헤더 대조, Win64 Development 패키징, 실제 게임 비활성 창 입력 및 사용자 입력 무간섭 |
| G2 | 명령 JSON Schema, bootstrap/session 토큰 분리, 1,000명령, 중복 방지, 이전 매핑·관측 번호 차단, Controller 프로세스 종료 시 750ms lease로 키 해제 및 새 세션 발급, 모의 런타임의 수동·자동·스크립트 입력 소유권 전환 | UE C++ 브리지와 실제 게임에서 인증·소유권·lease 검증 |
| G3–G4 | 모의 런타임 매핑, 입력 노드와 범위, 프리셋, JSON/YAML Action Catalog의 승인 상태·사전조건과 세 가지 테스트 범위, YAML scripted 시나리오의 단계별 판정 | 실제 활성 IMC·우선순위·축·리매핑 탐지, 게임별 Catalog 승인과 관측 어댑터 |
| G5 | 반복 선택, 후보 검증, 요청·시간·토큰·예상 비용 제한, 제한적 재시도 및 타임아웃, 매핑 변경 시 응답 폐기. 실제 요청 한 번에서 142개 입력 토큰을 집계하고 모델의 JUMP 선택을 모의 게임에서 실행 | 실제 게임 10분 자율 QA, 공급자 과금 상한 적용 및 운영 검증 |
| G6 | JSON/HTML, 모의 실행 코드 SHA-256, 입력 노드·반복 탐색·scripted 시나리오 Replay와 결과 비교 | 실제 UE 빌드 해시, 크래시 증거, 결함 재현 |
| G7 | Python 포함 Windows 모의 ZIP, 프로젝트 검색·진단 및 변경 승인 화면, 임시 프로젝트에서 설치·업그레이드·제거·충돌 보존·강제 종료 후 스테이징 복구, 소스 변경 fingerprint | 실제 UE 프로젝트의 플러그인과 Development 빌드, 최신 정상 빌드 승격 및 실제 설치·복구 검증 |
| G8 | 모의 앱 검증 | 두 번째 UE 프로젝트 이식, 30분 사용자 작업 병행 및 부하 측정, Shipping 차단 |

플러그인 설치 검증은 임시 파일에만 수행했다. 업데이트·제거는 설치 매니페스트와 현재 파일 해시가 일치할 때 승인할 수 있다. 소유권이 확인되지 않거나 수정된 플러그인은 보존한다. 중단된 트랜잭션은 복구 화면 또는 다음 승인 실행에서 앱 스테이징을 정리하고, 필요한 경우 백업을 돌려놓는다. BuildCookRun과 마지막 정상 빌드 승격에는 실제 엔진 검증이 필요하다.

로컬 회귀 테스트 23건과 Choice 응답 fixture 6건이 통과했다. 최신 ZIP을 임시 위치에 풀고 PATH에서 Python을 제외한 상태에서 Chrome 프리셋 왕복, 프로젝트 진단·미리보기·설치·제거, YAML 시나리오와 Replay 일치를 확인했다. ZIP SHA-256은 `f9f693a68a220e318df7f4a48cde1b3430cdb7a22cca948ea9532854b4423cdf`이다. 모의 패키지는 PyInstaller onedir 방식을 사용한다. [PyInstaller 사용 문서](https://pyinstaller.org/en/latest/usage.html)

예상 비용은 짧은 합성 요청의 사전 토큰 예약과 반환된 입력 토큰 사용량을 합산한다. 기본 단가는 Decisions의 비캐시 입력 기준이며, 프로젝트 청구 한도와 지역 할증은 실제 배포 설정에서 확인해야 한다. [OpenAI Decisions 가격 및 사용량](https://developers.openai.com/api/docs/guides/decisions#pricing-and-availability)

실제 키 값을 직접 확인·복사·출력하지 않았다. 실호출 한 번은 앱의 기존 인증 경로만 사용했다. 결과는 `runs/916d70edfdab4d6ba8840fd482e13af5.json`에 기록했다. 나머지 자동 테스트와 배포 검증은 실호출 없이 실행했다.
