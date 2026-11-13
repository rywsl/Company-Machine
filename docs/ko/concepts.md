[English](../en/concepts.md) | [日本語](../ja/concepts.md) | **한국어** | [Français](../fr/concepts.md) | [Deutsch](../de/concepts.md) | [Español](../es/concepts.md) | [Português (BR)](../pt-BR/concepts.md) | [Русский](../ru/concepts.md) | [简体中文](../zh-CN/concepts.md)

# 핵심 개념

Machine Agent Org의 메타모델은 작습니다. 만들 수 있는 모든 것(영상 파이프라인, 코드 리뷰 라인, 야간 보고서)은 아래 일곱 가지 프리미티브로 표현하며, 이들은 Git 레지스트리에 YAML **Spec**으로 작성됩니다. 구조 컴파일러는 타입 검사기가 코드를 검사하듯 이 Spec들을 검사합니다.

> 설계 근거: [`docs/architecture.md`](../architecture.md) 3–10절(중국어). 형식 규칙: [`spec/mrl/factory-profile.md`](../../spec/mrl/factory-profile.md).

## 1. 프리미티브

| 프리미티브 | 의미 | 핵심 |
|---|---|---|
| **Type(타입)** | 소재의 명세 | `name@major.minor`. 종류: 원자, `bundle{…}`, `list<T>`, `ref<T>`, `any`. 페이로드 레이아웃, 메타 스키마, validator(수락/거부 fixture 포함), 측정된 *사실*, 이름 붙은 *속성*을 가짐 |
| **Bean(소재)** | 소재 한 단위 | 내용 주소 방식(`sha256:…`), 불변, 계보와 출처 정보와 함께 봉인됨 |
| **Machine(머신)** | 기존 제품 | Machine Package로 기술됨: digest로 고정된 이미지, 레시피, 입력과 출력, 프로브, 적합성 골든 |
| **Operator(오퍼레이터)** | 머신을 조작하는 주체 | 에이전트 CLI(Claude Code 헤드리스, Codex), 스크립트, 또는 받은편지함을 통한 사람 |
| **Station(스테이션)** | Recipe × Operator × Policy | 타입이 지정된 포트. 역할: `source`, `transform`, `sink`. `converter`, `judge`, `planner`는 특수화된 transform |
| **Line(라인)** | 스테이션으로 이루어진 타입 있는 DAG | 여기에 상한이 있는 동적 구조가 더해짐. 인터페이스가 있는 라인은 스테이션처럼 쓸 수 있음(`subline`) |
| **Policy(정책)** | 권한 | 이그레스 등급, 시크릿, 예산, 승인, 게시, 보존 |

그 위에 메타 수준의 개념이 세 가지 있습니다. **Spec**은 위의 모든 정의를 가리키며, `spec.<kind>@N` 타입의 Bean으로 저장됩니다. **Invariant**(불변식)는 MRL로 작성한 헌법 규칙입니다. **ChangeSet**은 Spec의 diff이며, 그 자체도 Bean입니다. 공장 자체의 구조가 소재이기 때문에, 같은 검사로 유지보수 작업도 보호할 수 있습니다. Foreman(현장 감독)이 질서를 벗어나지 않게 하는 방법이 바로 이것입니다.

## 2. 타입, 사실, 속성

타입은 validator가 측정하는 *사실*(facts)과, 그 사실에 대한 이름 붙은 MRL 표현식인 *속성*(properties)을 선언합니다.

```yaml
# spec/examples/registry/types/video.yaml (발췌)
kind: Type
metadata: {name: video.mp4, version: "1.3"}
spec:
  extends: video@1
  payload:
    - {path: video.mp4, media_type: video/mp4, required: true}
  validators:
    - name: ffprobe-facts
      run: [probe-video, /bean/payload/video.mp4]
      emits: [duration_s, width, height, fps, vcodec, audio_streams, mean_volume_db]
      fixtures:
        accept: [fixtures/video/ok-1920x1080.mp4]
        reject: [fixtures/video/truncated.mp4, fixtures/video/png-renamed.mp4]
  properties:
    has_audio: "bean.facts.audio_streams > 0"
    vertical:  "bean.facts.height > bean.facts.width"
    hd:        "(bean.facts.width < bean.facts.height ? bean.facts.width : bean.facts.height) >= 1080"
```

모든 validator에는 수락 fixture와 거부 fixture가 함께 제공됩니다. 거부 fixture를 통과시키는 변경, 즉 불량 소재를 들여보내게 되는 변경은 자동으로 고위험으로 분류됩니다.

## 3. 소비자 계약: `guarantees`와 `requires`

포트는 속성을 기준으로 계약을 명시합니다. 출력 포트는 자신이 **보장**하는 것(`guarantees`)을 나열하고, 게이트(gate, 출하 검사)가 이를 강제합니다. 입력 포트는 자신이 **요구**하는 것(`requires`)을 나열합니다.

```yaml
# spec/examples/registry/stations/vertical.yaml
kind: Station
metadata: {name: vertical, version: 1}
spec:
  role: transform
  machine: ffmpeg@3
  recipe: reframe-vertical
  operator: {runtime: script@1}
  inputs:  {video: {type: video.mp4@1, requires: [hd, has_audio]}}
  outputs: {video: {type: video.mp4@1, guarantees: [vertical, hd, h264, has_audio, not_silent]}}
  limits:  {timeout: 15m, budget_usd: 0}
  policy: tool-default@1
```

컴파일 시점에 모든 엣지는 `requires ⊆ guarantees ∪ type properties`를 만족해야 합니다. 이는 집합 포함 관계이므로, 업스트림 스테이션이 보장 하나를 빼면 컴파일러가 어떤 다운스트림 포트가 깨지는지 정확히 짚어 줍니다. 실행 시점에는 입력 검사가 실제 Bean에 대해 같은 속성을 평가합니다.

## 4. 변환: 산출물이 자동으로 소재가 된다

엣지의 타입이나 속성이 맞지 않으면, 컴파일러는 레지스트리에서 **컨버터** 스테이션(`converter`)을 찾아 정확히 하나의 체인을 끼워 넣습니다. 조건에 맞는 체인이 여럿이면 다음 순서의 규칙으로 고릅니다. 무손실 우선, 그다음 단계 수가 적은 것, 추정 비용이 낮은 것, 추정 시간이 짧은 것, 마지막으로 이름순입니다. [시작하기](getting-started.md)에서 본 것처럼 `note.raw@1 → note.text@1`은 `note-to-text@1`이 이어 주었습니다. 체인이 없으면 컴파일러는 추측하지 않습니다. `E-NO-CONVERTER`를 보고하고, *변환 요청*이 Foreman을 깨워 새 컨버터를 제안하게 합니다.

## 5. 라인

```yaml
# spec/examples/registry/lines/explainer-multilang.yaml (발췌)
kind: Line
metadata: {name: explainer-multilang, version: 5}
spec:
  interface:
    inputs: {brief: {type: brief.video@1}}
  trigger:
    - {on: bean, type: brief.video@1, label: "line=explainer-multilang"}
  budget: {cap_usd: 12.0}
  steps:
    explainer: {station: explainer@3, in: {brief: $inputs.brief}}
    dub_en:    {station: dub@2, params: {target_language: en},
                in: {video: explainer.video, subtitle: explainer.subtitle}}
    vertical:  {station: vertical@1, in: {video: dub_en.video}}
    to_s3:     {station: publish-s3@2, params: {bucket: media-archive, prefix: "explainers/{{ run.id }}/"},
                in: {artifact: vertical.video}}
```

규칙:
- 모든 최종 산출물은 싱크(sink, 외부로 내보내는 스테이션)가 소비하거나 `keep`에 나열되어야 합니다.
- 예산은 계층적입니다: 전역 → 라인 → 실행 → 스테이션 → job.
- 트리거는 `bean` 도착, `cron` 또는 `every` 스케줄, 수동 실행 중 하나입니다.

### 상한이 있는 동적 구조

| 구성 요소 | 하는 일 | 상한 |
|---|---|---|
| `map` | `list<T>`의 각 항목마다 하위 단계를 실행 | `max_items`(초과 시 거부 또는 잘라냄), `max_parallel` |
| `switch` | MRL 조건으로 분기 하나를 선택 | 망라적이어야 하며 `default` 필수 |
| `until` | MRL 조건이 참이 될 때까지 하위 단계를 반복 | `max_iter`. 모두 소진하면 실행이 실패 |
| `subline` | 현재 실행의 에포크 안에서 다른 라인을 실행 | 깊이 제한 |
| `plan` | **planner** 스테이션이 `Plan`(하위 DAG)을 내보냄 | 무엇이든 실행되기 전에 V1/V2로 컴파일되고 `plan_bounds`와 대조됨 |

## 6. 에포크: 실행 중인 작업을 깨뜨리지 않고 변경하기

검증을 통과한 레지스트리 커밋은 모두 **에포크**(epoch, 고정된 구조 버전)가 될 수 있습니다. 실행은 시작할 때 에포크 하나에 묶이고 끝날 때까지 그 에포크를 유지하므로, 구조를 바꿔도 실행 중인 작업에는 절대 영향이 없습니다. 롤백은 현재 에포크 포인터를 뒤로 옮기는 것을 뜻합니다. 여러 에포크가 동시에 살아 있을 수 있으며, 예를 들어 활성 에포크와 카나리 에포크가 나란히 존재할 수 있습니다.

## 7. 6계층 검증

| 계층 | 검사 내용 | 시점 | 실패 시 |
|---|---|---|---|
| **V1** Spec | 스키마, 참조, 고정된 버전, 정책 lint | 모든 제안 | 변경 거부 |
| **V2** 구조 | 엣지 타입과 계약, 순환, 필수 입력, 출력 소비 여부, 권한, 예산, 동적 구조 상한 | 제안 + 매 실행 전 | 변경 / 실행 거부 |
| **V3** 진화 | 이전 버전과의 호환성, 모든 다운스트림 소비자 계약, 영향 범위 | 제안 | 거부 또는 마이그레이션 컨버터 요구 |
| **V4** 계약 테스트 | 머신 적합성, 골든 brief, 컨버터 왕복 변환, 싱크 드라이 런 | 제안 + 정기 카나리 | 거부 또는 드리프트 경보 |
| **V5** 런타임 | 입력 검사, 게이트(구조 → validator → 수락 → judge), 싱크 영수증 검증 | 모든 job | 거부 → 재시도, 또는 DLQ |
| **V6** 지속 | 불변식 감사, 드리프트 모니터링, CAS와 계보 무결성, 리컨실 | 주기적 + 이벤트 | 격리, 서킷 브레이크, 롤백, Foreman 깨우기 |

## 8. 네 가지 드리프트

| 드리프트 | 예시 | 탐지 수단 |
|---|---|---|
| Spec 드리프트 | 누군가 타입이나 포트를 수정함 | V1–V3와 소비자 계약 |
| 동작 드리프트 | 제품이 업그레이드되거나 모델·프롬프트가 바뀜 | 고정된 버전, V4 카나리, 섀도 실행 |
| 의미 드리프트 | 출력은 여전히 스키마를 통과하지만 품질이 떨어짐 | 수락 표현식, 샘플링 judge, 지표 기준선 |
| 설정 드리프트 | 컨테이너나 시크릿 바인딩이 정식 절차 밖에서 수정됨 | 리컨실러와 불변식 감사 |

## 9. 거버넌스 티어

검증 파이프라인은 각 ChangeSet의 diff에서 리스크 티어를 결정론적으로 도출합니다. Foreman을 포함해 누구도 이를 낮출 수 없습니다.

| 티어 | 대표적인 변경 | 요구 사항 |
|---|---|---|
| T0 | 재시도, 격리 | 자동 |
| T1 | 템플릿 조정, 새 컨버터, 타입 minor 버전 올림 | 검증 + 섀도 실행 |
| T2 | 머신 도입 또는 업그레이드, 새 라인, 타입 major 버전 올림 | + 카나리 + 다른 런타임에 의한 교차 리뷰 |
| T3 | 새 소스/싱크, 이그레스·자격 증명 바인딩, 정책 변경, 퇴역 | + 사람의 승인 |
| T4 | 헌법 | 사람만 가능 |

헌법(L0)은 최종 기준점입니다. 머신과 에이전트는 그 밖의 모든 것을 다시 구성할 수 있지만, 반드시 검증되고 리뷰 가능하며 되돌릴 수 있는 ChangeSet을 통해서만 가능합니다.
