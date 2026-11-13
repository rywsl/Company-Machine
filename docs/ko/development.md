[English](../en/development.md) | [日本語](../ja/development.md) | **한국어** | [Français](../fr/development.md) | [Deutsch](../de/development.md) | [Español](../es/development.md) | [Português (BR)](../pt-BR/development.md) | [Русский](../ru/development.md) | [简体中文](../zh-CN/development.md)

# 개발

Machine Agent Org는 **설계자 / 구현자** 역할을 명시적으로 나누어 개발합니다. 결정론적인 harness인 `tools/harness.py`가 이를 강제하므로, 어느 쪽도 몰래 기준을 옮길 수 없습니다.

## 1. 역할

| 역할 | 담당 | 할 수 없는 일 |
|---|---|---|
| **설계자** | 명세(`spec/`), 계획과 태스크 카드(`plan/`), 인수 테스트(`tests/acceptance/`), 리뷰 | 프로덕션 코드 구현 |
| **구현자** | `src/`, 시스템 테스트(`tests/system/`), 저널 항목 | 명세, 인수 테스트, 인수 기준 변경. 인터페이스 창작. 마일스톤보다 앞서 작업 |
| **사람** | 헌법, ADR, 그리고 **보호 파일 재등록**(`harness lock --update`) | — |

보호 파일은 `plan/protected.lock`에 glob으로 나열되고 SHA-256으로 고정됩니다. PreToolUse 훅(`.claude/settings.json`)이 에이전트의 보호 파일 편집을 거부하고, `harness lock --verify`가 모든 변경을 탐지합니다. 구현자가 모순을 발견하면 작업을 멈추고 `plan/journal/`에 기록한 뒤 `harness task block`을 실행합니다. 설계자가 보호 파일을 고치고, 사람이 잠금을 다시 등록합니다.

## 2. harness 루프

```bash
uv run python tools/harness.py doctor            # 환경
uv run python tools/harness.py status            # 마일스톤, 태스크, 게이트
uv run python tools/harness.py task next         # 의존성이 모두 끝난 첫 번째 태스크
uv run python tools/harness.py task show M9-01   # 태스크 카드와 필독 자료
uv run python tools/harness.py task start M9-01
# … 구현 …
uv run python tools/harness.py accept M9-01      # 태스크의 인수 검사를 실행하고 증거를 기록
uv run python tools/harness.py gate M9           # 스테이지 게이트: 마일스톤의 모든 검사(상속된 검사 포함)
```

기타 하위 명령:
- `doccheck`: 명세 7장의 테스트 표가 테스트 함수와 일치해야 합니다.
- `check`: 이름을 지정한 검사 하나.
- `scenario`: `plan/scenarios/`의 스크립트형 시나리오.
- `pytest`: pytest를 실행하고 JSON 요약을 출력합니다. `tests` 패키지의 import 경로를 설정하므로, 그냥 `pytest` 대신 항상 이것을 사용하세요.
- `lock --verify`: 보호 파일을 잠금과 비교합니다.

증거(명령, 종료 코드, 출력 다이제스트, git 커밋)는 `plan/state.json`에 기록됩니다.

## 3. 마일스톤

```mermaid
flowchart LR
    M0["M0<br/>구조 컴파일러"] --> M1["M1<br/>단일 스테이션"] --> M2["M2<br/>라인과 외부 세계"] --> M3["M3<br/>거버넌스와 Foreman"] --> M4["M4<br/>동적 구조와 프로덕션"]
    M4 --> M5["M5<br/>자율 루프"] --> M6["M6<br/>실제 에이전트 CLI"] --> M7["M7<br/>운영"] --> M8["M8<br/>리뷰 수정"] --> M9["M9<br/>마무리 수정"]
```

모든 마일스톤은 바로 쓸 수 있고 인계 가능한 상태로 끝나며, 모든 스테이지 게이트는 이전 게이트의 검사를 상속합니다. M5, M8, M9는 *교정* 마일스톤입니다. 각각 테스트는 통과했지만 프로덕션 경로에 연결되지 않은 기능을 찾아낸 설계자 리뷰에서 비롯되었습니다(`plan/drift.md`, `plan/review-m5-m7.md`, `plan/review-closure.md`). 여기서 얻은 교훈은 이제 인수 테스트의 규칙이 되었습니다. **프로덕션 진입점을 구동하고, 가짜 구현이 동작을 검증하게 하라.** 가짜 S3는 SigV4를 다시 계산하고, 가짜 `psql`은 모든 호출을 기록하며, 가짜 `factoryctl`은 실제 메서드와 경로를 그대로 전달합니다.

## 4. 테스트

| 계층 | 위치 | 내용 |
|---|---|---|
| 인수 | `tests/acceptance/m0…m9/` | 태스크마다 파일 하나, 명세 7장에 이름이 명시됨. 보호됨 |
| 시스템 | `tests/system/` | 구현자가 담당하는 단위·통합 테스트 |
| 적합성 | `spec/mrl/conformance/`, `tests/test_conformance.py` | MRL 표현식, 규칙, 공장 케이스. 기존 케이스는 추가만 가능 |
| 정책 | `tests/test_codebase_policy.py`, `tests/test_hooks.py` | `src/mao`에 대한 정적 규칙(몽키 패칭 import 금지, 현재 디렉터리 접근 금지, 모듈 간 private 접근 금지 등) |

테스트 대역은 `tests/fakes/`에 있습니다.
- `fake_agent.py`는 Claude Code를 흉내 내는 스크립트형 대역입니다. 그 동작은 태스크 Bean의 `task.json`으로 정해집니다.
- `fake_llm.py`는 live 계층을 위한 모델 업스트림입니다.
- `tools/`에는 fixture 도구가 들어 있습니다.

fixture 레지스트리는 `tests/fixtures/registry-m1`과 `registry-m2`입니다. Docker 계층에는 `uv run python tools/build_test_images.py`가 필요합니다. Docker가 없으면 건너뛰며, `MAO_REQUIRE_DOCKER=1`을 설정하면 건너뛰는 대신 실패합니다. 스테이지 게이트는 건너뜀 0건을 요구합니다.

## 5. 코드 규칙

이 규칙들은 `spec/system/README.md`(결정 D1–D14)와 `plan/README.md` § 6에서 왔습니다.

- Python ≥ 3.12. 런타임 의존성은 표준 라이브러리, `pyyaml`, `jsonschema`, 저장소 안의 `mrl`뿐입니다. 새 의존성을 추가하려면 ADR이 필요합니다. Docker CLI는 하위 프로세스로, SQLite는 `sqlite3`로 호출합니다.
- `shell=True` 금지. LLM API 호출 금지: 지능은 오직 오퍼레이터 CLI에서 나옵니다.
- **MRL이 유일한 표현식·템플릿 언어입니다.** Spec 템플릿은 `mrl.expr`로 컴파일하며, Spec 내용에 `eval`, Jinja, `str.format`을 쓰는 것은 금지됩니다.
- 라이브러리 코드는 경로를 `Context.root`나 호출자에게서 받으며, 현재 디렉터리에서 가져오지 않습니다.
- 런타임 SQL은 SQLite와 PostgreSQL 사이에서 이식 가능해야 합니다. SQLite 전용 구문은 `db.py`, `backends.py`, `backup.py`에서만 허용됩니다.
- `uv run ruff check src tests tools`와 `uv run ruff format --check`를 통과해야 하며, 줄 길이는 110입니다.

## 6. 더 읽을거리

| 주제 | 문서 |
|---|---|
| 워크플로 규칙, 저널 형식, 금지 행위 | [`plan/README.md`](../../plan/README.md) |
| 마일스톤과 완료 기준 | [`plan/milestones.md`](../../plan/milestones.md), [`plan/gates.yaml`](../../plan/gates.yaml) |
| 컴포넌트 간 결정과 규약 | [`spec/system/README.md`](../../spec/system/README.md) |
| MRL 언어 | [`spec/mrl/mrl-1.0.md`](../../spec/mrl/mrl-1.0.md), [`spec/mrl/factory-profile.md`](../../spec/mrl/factory-profile.md) |
| 구현자 지침 | [`AGENTS.md`](../../AGENTS.md) |
