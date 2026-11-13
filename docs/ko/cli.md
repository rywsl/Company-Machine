[English](../en/cli.md) | [日本語](../ja/cli.md) | **한국어** | [Français](../fr/cli.md) | [Deutsch](../de/cli.md) | [Español](../es/cli.md) | [Português (BR)](../pt-BR/cli.md) | [Русский](../ru/cli.md) | [简体中文](../zh-CN/cli.md)

# CLI 레퍼런스

진입점은 두 개이며, 둘 다 `uv sync`로 설치됩니다.

| 명령 | 패키지 | 용도 |
|---|---|---|
| `factory` | `mao.cli:main` | 공장 컨트롤 플레인 |
| `mrl` | `mrl.cli:main` | MRL(MAO Relation Language) 툴킷: 평가, 검사, 진단 설명 |

샌드박스 안에서 오퍼레이터는 세 번째 도구인 `factoryctl`을 사용합니다. [런타임 § 3](runtime.md#3-스테이션-내부)을 참고하세요.

정확한 인수는 각 명령을 `--help`와 함께 실행해 확인하세요. 규범이 되는 명령 목록은 [`spec/system/commands.md`](../../spec/system/commands.md)입니다.

## 전역 옵션

| 옵션 | 환경 변수 | 기본값 | 의미 |
|---|---|---|---|
| `--root DIR` | `FACTORY_ROOT` | `.factory` | 공장 상태 디렉터리 |
| `--registry DIR` | `FACTORY_REGISTRY` | `registry` | 레지스트리(Spec) 디렉터리 |
| `--provider docker\|local` | | `docker` | 샌드박스 프로바이더 |
| `--local-commands FILE` | | | 로컬 프로바이더용 명령 이름 → 호스트 명령 매핑 |
| `--epoch COMMIT` | | 활성 에포크 | 특정 에포크를 대상으로 실행 |
| `--json` | | | 기계가 읽을 수 있는 출력 |
| `--version` | | | 버전 출력 |

## 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 성공 |
| 1 | 업무상 실패: job `DEAD`, 실행 `FAILED`/`CANCELLED`, 계약 판정 `fail`, 미해결 감사 발견 사항 |
| 2 | 사용법 오류 |
| 3 | 유효하지 않은 레지스트리(`REGISTRY_INVALID`) |
| 4 | 환경 또는 설비 오류(예: Docker를 사용할 수 없음) |
| 5 | 내부 오류 |

## 구조와 거버넌스

| 명령 | 설명 |
|---|---|
| `check ROOT` | 레지스트리를 불러와 컴파일. 오류가 있으면 종료 코드 3 |
| `epoch init COMMIT` · `ls` · `show [COMMIT]` · `rollback [COMMIT]` · `adopt COMMIT` | 에포크와 활성 포인터 관리 |
| `changeset propose FILE --commit C --by ACTOR` · `ls [--state S]` · `show ID` · `withdraw ID --by ACTOR` · `advance ID` | ChangeSet 파이프라인 |
| `governance tick` · `governance safe-mode` | ChangeSet을 한 번 진행. 안전 모드를 표시하거나 평가 |
| `contract validator …` · `golden …` · `equivalence …` · `diff …` · `sink-dry-run …` · `ls` | V4 계약 테스트 |
| `audit once` · `audit ls [--state S]` | 불변식 감사 실행 / 발견 사항 목록 |
| `drift ls` | 드리프트 발견 사항 |

## 소재

| 명령 | 설명 |
|---|---|
| `bean import --type TYPE --file PATH=HOST …` | 외부 파일을 Bean으로 받아들임. Bean의 id를 출력 |
| `bean lineage BEAN --direction up\|down` | 업스트림 또는 다운스트림의 job과 Bean |
| `bean label BEAN KEY=VALUE` | 레이블 추가(레이블은 라인을 트리거할 수 있음) |
| `cache ls [--station S@N]` · `explain JOB` · `invalidate (--key\|--job\|--station\|--bean) … --reason TEXT` | job 캐시 |
| `gc [--dry-run]` | 보존: 톰스톤, blob 정리, 작업 공간 회수 |

## 실행

| 명령 | 설명 |
|---|---|
| `job run STATION --input PORT=BEAN …` | job 하나를 제출해 명령 프로세스 안에서 실행(종료 코드 0 봉인, 1 DEAD) |
| `job show JOB` · `job exec JOB` | job 조회. 제출된 job의 리스를 잡고 실행(serve의 워커 하위 프로세스가 사용) |
| `line plan LINE@N` | 라인을 실행 계획(노드, 컨버터, 예산)으로 컴파일 |
| `line run LINE@N --in PORT=BEAN … [--budget USD] [--variant STEP=V] [--no-cache]` | 실행을 시작해 완료까지 진행 |
| `line ls` · `show RUN` · `nodes RUN` · `cancel RUN` · `retry RUN` · `rebuild RUN` | 실행 조회와 제어 |
| `trigger process` | 라인 트리거를 한 번 평가 |
| `schedule tick` · `dlq ls` · `quarantine BEAN --reason TEXT` · `release BEAN` | 스케줄러, 데드 레터, 격리 |
| `approval ls [--job J] [--status S]` · `show ID` · `decide ID approve\|deny --by NAME [--note TEXT]` | 승인 지점 |

## 통합

| 명령 | 설명 |
|---|---|
| `image build …` · `image ls` | 머신 이미지 빌드와 목록(Foreman이 작성한 Dockerfile) |
| `gateway manifest MACHINE@V` | 형태 B 머신의 서비스 게이트웨이 매니페스트 |
| `egress check STATION@N` | 스테이션에 실제로 적용되는 이그레스 계획 표시 |
| `secret ls` | 시크릿 이름과 등급(값은 절대 표시하지 않음) |
| `backend status` · `migrate …` · `verify` | 스토리지 백엔드: 로컬 ↔ S3 + Postgres |
| `backup create` · `verify` · `restore` | 상태와 blob 백업 |

## 자율

| 명령 | 설명 |
|---|---|
| `serve [--once]` | 데몬: 트리거, 실행, job, 거버넌스, 감사, 카나리, 웨이크 티켓 |
| `foreman next` · `done WAKE` · `get KEY` · `list KIND` · `diff BASE COMMIT` · `bean …` · `job …` · `dlq` · `drift` · `invariant` · `metrics` · `approval` · `changeset` · `review` · `conversion` · `supervise` · `sessions` · `wakes` · `reopen` | Foreman(현장 감독)의 작업 인터페이스 |
| `mcp --role foreman\|reviewer` | 같은 인터페이스를 stdio 기반 MCP 서버로 제공 |
| `maintain bootstrap` | 유지보수 라인과 `registry-sink`가 있는지 확인. 없으면 종료 코드 1과 누락 목록 |

## 관측성

| 명령 | 설명 |
|---|---|
| `status` | 공장 전체의 JSON 스냅샷 하나 |
| `metrics` | Prometheus 텍스트 형식 |
| `events [--since SEQ] [--kind K] [--limit N]` | 이벤트 로그 |
| `console [--port N]` | 로컬 읽기 전용 웹 콘솔(기본값 `127.0.0.1:8090`) |

## `mrl`

| 명령 | 설명 |
|---|---|
| `mrl eval EXPR` · `mrl template TEXT` | 표현식 평가 / 템플릿 렌더링 |
| `mrl check MODULE` · `mrl run MODULE` | 규칙 모듈 정적 검사 / 평가 |
| `mrl registry …` | 레지스트리 검사와 조회(`mrl registry check --strict`) |
| `mrl resolve …` | 가상의 엣지에 대한 변환 경로 해석 |
| `mrl explain CODE` | `E-NO-CONVERTER` 같은 진단 코드 설명 |
| `mrl conformance` · `mrl coverage` | 적합성 스위트 실행 / 공장 케이스 커버리지 표시 |
