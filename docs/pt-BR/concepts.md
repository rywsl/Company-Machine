[English](../en/concepts.md) | [日本語](../ja/concepts.md) | [한국어](../ko/concepts.md) | [Français](../fr/concepts.md) | [Deutsch](../de/concepts.md) | [Español](../es/concepts.md) | **Português (BR)** | [Русский](../ru/concepts.md) | [简体中文](../zh-CN/concepts.md)

# Conceitos

O Machine Agent Org tem um metamodelo pequeno. Tudo o que você pode construir (um pipeline de vídeo, uma linha de revisão de código, um relatório noturno) é expresso com as sete primitivas abaixo, escritas como **Specs** em YAML em um registro Git. O compilador de estrutura verifica essas Specs do mesmo jeito que um verificador de tipos verifica código.

> Justificativa do design: seções 3–10 de [`docs/architecture.md`](../architecture.md) (em chinês). Regras formais: [`spec/mrl/factory-profile.md`](../../spec/mrl/factory-profile.md).

## 1. As primitivas

| Primitiva | Significado | Pontos-chave |
|---|---|---|
| **Type** (tipo) | a especificação de um material | `name@major.minor`. Categorias: atômico, `bundle{…}`, `list<T>`, `ref<T>`, `any`. Traz um layout de payload, um schema de metadados, validadores (com fixtures de aceitação e rejeição), *fatos* medidos e *propriedades* nomeadas |
| **Bean** (material) | uma porção de material | endereçado por conteúdo (`sha256:…`), imutável, selado com linhagem e procedência |
| **Machine** (máquina) | um produto existente | descrita por um Machine Package: imagem fixada por digest, receitas, entradas e saídas, sondas, referências de conformidade |
| **Operator** (operador) | quem opera a máquina | um agente CLI (Claude Code headless, Codex), um script ou um humano por meio de uma caixa de entrada |
| **Station** (estação) | Recipe × Operator × Policy | portas tipadas. Papéis: `source`, `transform`, `sink`; `converter`, `judge` e `planner` são transformações especializadas |
| **Line** (linha) | um DAG tipado de estações | mais construções dinâmicas limitadas. Uma linha com interface pode ser usada como estação (`subline`) |
| **Policy** (política) | permissões | nível de saída de rede, segredos, orçamento, aprovações, publicação, retenção |

Acima delas ficam três conceitos de nível meta. Uma **Spec** é qualquer uma das definições acima, armazenada como um bean do tipo `spec.<kind>@N`. Um **Invariant** é uma regra da constituição escrita em MRL. Um **ChangeSet** é um diff de Specs e é, ele próprio, um bean. Como a própria estrutura da fábrica é material, as mesmas verificações podem proteger o trabalho de manutenção. É assim que o Foreman (capataz) é mantido na linha.

## 2. Tipos, fatos e propriedades

Um tipo declara *fatos*, que os validadores medem, e *propriedades*, que são expressões MRL nomeadas sobre esses fatos.

```yaml
# spec/examples/registry/types/video.yaml (trecho)
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

Cada validador vem com fixtures de aceitação e de rejeição. Uma mudança que faça uma fixture de rejeição passar, o que deixaria material ruim passar, é classificada automaticamente como de alto risco.

## 3. Contratos de consumidor: `guarantees` e `requires`

As portas declaram contratos em termos de propriedades. Uma porta de saída lista o que ela **garante** (`guarantees`), e o gate (controle de saída) faz cumprir. Uma porta de entrada lista o que ela **exige** (`requires`).

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

Em tempo de compilação, toda aresta precisa satisfazer `requires ⊆ guarantees ∪ type properties`. Trata-se de uma inclusão de conjuntos: quando uma estação a montante deixa de oferecer uma garantia, o compilador aponta exatamente quais portas a jusante quebram. Em tempo de execução, a verificação de entrada avalia as mesmas propriedades sobre o bean real.

## 4. Conversão: produtos viram material automaticamente

Quando os tipos ou as propriedades de uma aresta não batem, o compilador procura no registro estações **conversoras** (`converter`) e insere exatamente uma cadeia. Quando várias cadeias se qualificam, ele escolhe por estas regras, nesta ordem: sem perdas primeiro, depois menos etapas, depois menor custo estimado, depois menor tempo estimado e, por fim, o nome. Você viu isso em [Primeiros passos](getting-started.md): `note.raw@1 → note.text@1` foi ligado por `note-to-text@1`. Quando não existe cadeia, o compilador não chuta. Ele reporta `E-NO-CONVERTER`, e um *pedido de conversão* desperta o Foreman, que pode propor um novo conversor.

## 5. Linhas

```yaml
# spec/examples/registry/lines/explainer-multilang.yaml (trecho)
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

As regras:
- Todo produto terminal precisa ser consumido por um sink (estação de publicação) ou listado em `keep`.
- Os orçamentos são hierárquicos: global → linha → execução → estação → job.
- Os gatilhos podem ser chegadas de `bean`, agendamentos `cron` ou `every`, ou acionamento manual.

### Estrutura dinâmica limitada

| Construção | O que faz | Limite |
|---|---|---|
| `map` | roda uma subetapa para cada item de uma `list<T>` | `max_items` (rejeitar ou truncar no estouro), `max_parallel` |
| `switch` | escolhe um ramo por uma condição MRL | precisa ser exaustivo, com um `default` |
| `until` | repete uma subetapa até uma condição MRL ser satisfeita | `max_iter`; esgotá-lo faz a execução falhar |
| `subline` | roda outra linha dentro da epoch desta execução | limite de profundidade |
| `plan` | uma estação **planner** emite um `Plan` (um sub-DAG) | compilado com V1/V2 e conferido contra `plan_bounds` antes que qualquer coisa rode |

## 6. Epochs: mudar sem quebrar o trabalho em andamento

Todo commit do registro que passa na verificação pode se tornar uma **epoch** (uma versão congelada da estrutura). Uma execução se vincula a uma epoch ao começar e a mantém até terminar, então mudar a estrutura nunca afeta o trabalho em andamento. Fazer rollback significa mover o ponteiro da epoch atual de volta. Várias epochs podem estar em operação ao mesmo tempo, por exemplo a epoch ativa ao lado de um canário.

## 7. Seis camadas de verificação

| Camada | O que é verificado | Quando | Em caso de falha |
|---|---|---|---|
| **V1** Spec | schema, referências, versões fixadas, lint de políticas | a cada proposta | rejeita a mudança |
| **V2** Estrutura | tipos e contratos das arestas, ciclos, entradas obrigatórias, saídas consumidas, permissões, orçamento, limites dinâmicos | proposta + antes de cada execução | rejeita a mudança / a execução |
| **V3** Evolução | compatibilidade com a versão anterior, todos os contratos dos consumidores a jusante, raio de impacto | proposta | rejeita, ou exige um conversor de migração |
| **V4** Testes de contrato | conformidade das máquinas, briefs de referência, idas e voltas dos conversores, simulações dos sinks | proposta + canários agendados | rejeita, ou dispara um alerta de deriva |
| **V5** Runtime | verificação de entrada, gate (estrutura → validador → aceitação → juiz), verificação dos recibos dos sinks | cada job | rejeição → nova tentativa, ou DLQ |
| **V6** Contínua | auditoria de invariantes, monitoramento de deriva, integridade do CAS e da linhagem, reconciliação | periódica + eventos | quarentena, circuit breaker, rollback, despertar o Foreman |

## 8. Quatro tipos de deriva

| Deriva | Exemplo | Detectada por |
|---|---|---|
| Deriva de spec | alguém edita um tipo ou uma porta | V1–V3 e contratos de consumidor |
| Deriva de comportamento | o produto é atualizado, o modelo ou o prompt muda | versões fixadas, canários V4, execuções shadow |
| Deriva semântica | a saída ainda passa no schema, mas a qualidade cai | expressões de aceitação, juízes por amostragem, linhas de base de métricas |
| Deriva de configuração | contêineres ou vínculos de segredos editados fora do processo | o reconciliador e a auditoria de invariantes |

## 9. Níveis de governança

O pipeline de verificação deriva, de forma determinística, um nível de risco do diff de cada ChangeSet. Ninguém pode reduzi-lo, nem mesmo o Foreman.

| Nível | Mudança típica | Requisitos |
|---|---|---|
| T0 | nova tentativa, quarentena | automático |
| T1 | ajuste de template, novo conversor, versão minor de tipo | verificação + execução shadow |
| T2 | incorporar ou atualizar uma máquina, nova linha, versão major de tipo | + canário + revisão cruzada por um runtime diferente |
| T3 | nova source/sink, saída de rede ou vínculo de credenciais, mudança de política, desativação | + aprovação humana |
| T4 | a constituição | somente humanos |

A constituição (L0) é a âncora final. Máquinas e agentes podem remodelar todo o resto, mas apenas por meio de ChangeSets verificados, revisáveis e reversíveis.
