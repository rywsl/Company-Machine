[English](../en/README.md) | [日本語](../ja/README.md) | **한국어** | [Français](../fr/README.md) | [Deutsch](../de/README.md) | [Español](../es/README.md) | [Português (BR)](../pt-BR/README.md) | [Русский](../ru/README.md) | [简体中文](../zh-CN/README.md)

# 문서

<p align="center"><img src="../assets/architecture.svg" alt="아키텍처" width="100%"></p>

이 가이드들은 Machine Agent Org를 사용하는 방법과 개발에 참여하는 방법을 설명합니다. 규범이 되는 출처는 명세입니다. 컨트롤 플레인은 [`spec/system/`](../../spec/system/README.md), MRL 언어는 [`spec/mrl/`](../../spec/mrl/)를 보세요. 설계의 전체 근거는 중국어로 작성된 [`docs/architecture.md`](../architecture.md)에 있습니다. 가이드와 명세가 다를 때는 명세가 우선합니다.

| 가이드 | 이럴 때 읽으세요 |
|---|---|
| [시작하기](getting-started.md) | 10분 정도에 설치·확인하고 첫 라인을 실행해 보고 싶을 때 |
| [핵심 개념](concepts.md) | Bean, 타입, 머신, 스테이션, 라인, 계약, 에포크, 검증 계층을 이해하고 싶을 때 |
| [런타임](runtime.md) | 실행 시점에 무슨 일이 일어나는지 알고 싶을 때: 프로세스, job 생명주기, 샌드박스, 저장소, 관측성, 알려진 미비점 |
| [CLI 레퍼런스](cli.md) | `factory`나 `mrl` 명령을 찾아보고 싶을 때 |
| [개발](development.md) | 개발에 참여하고 싶을 때: harness 워크플로, 마일스톤, 보호 파일, 테스트 |

## 추천 읽기 순서

1. **처음 사용하는 분:** 시작하기 → 핵심 개념 → CLI 레퍼런스.
2. **공장을 무인으로 운영하는 분:** 런타임(`serve`, 관측성, 백업 절) → CLI 레퍼런스.
3. **기여자:** 개발 → 핵심 개념 → 수정할 컴포넌트의 명세.

## 다른 언어

English: [README](../../README.md) · [docs/en](../en/README.md) | 日本語: [README](../../README_ja.md) · [docs/ja](../ja/README.md) | 한국어: [README](../../README_ko.md) · [docs/ko](../ko/README.md) | Français: [README](../../README_fr.md) · [docs/fr](../fr/README.md) | Deutsch: [README](../../README_de.md) · [docs/de](../de/README.md) | Español: [README](../../README_es.md) · [docs/es](../es/README.md) | Português (BR): [README](../../README_pt-BR.md) · [docs/pt-BR](../pt-BR/README.md) | Русский: [README](../../README_ru.md) · [docs/ru](../ru/README.md) | 简体中文: [README](../../README_zh-CN.md) · [docs/zh-CN](../zh-CN/README.md)
