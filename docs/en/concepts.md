**English** | [简体中文](../zh-CN/concepts.md) | [日本語](../ja/concepts.md)

# Concepts

Machine Agent Org has a small meta-model. Everything you can build (a video pipeline, a code-review line, a nightly report) is expressed with the seven primitives below, written as YAML **Specs** in a Git registry. The structure compiler checks those Specs the same way a type checker checks code.

> Design rationale: [`docs/architecture.md`](../architecture.md) sections 3–10 (Chinese). Formal rules: [`spec/mrl/factory-profile.md`](../../spec/mrl/factory-profile.md).

## 1. The primitives

| Primitive | Meaning | Key points |
|---|---|---|
| **Type** | the specification of a material | `name@major.minor`. Kinds: atomic, `bundle{…}`, `list<T>`, `ref<T>`, `any`. Carries a payload layout, a meta schema, validators (with accept/reject fixtures), measured *facts* and named *properties* |
| **Bean** | one piece of material | content-addressed (`sha256:…`), immutable, sealed with lineage and provenance |
| **Machine** | an existing product | described by a Machine Package: image pinned by digest, recipes, inputs and outputs, probes, conformance goldens |
| **Operator** | who drives the machine | an agent CLI (Claude Code headless, Codex), a script, or a human through an inbox |
| **Station** | Recipe × Operator × Policy | typed ports. Roles: `source`, `transform`, `sink`; `converter`, `judge` and `planner` are specialised transforms |
| **Line** | a typed DAG of stations | plus bounded dynamic constructs. A line with an interface can be used as a station (`subline`) |
| **Policy** | permissions | egress tier, secrets, budget, approvals, publishing, retention |

Three meta-level concepts sit on top. A **Spec** is every definition above, stored as a bean of type `spec.<kind>@N`. An **Invariant** is a constitution rule written in MRL. A **ChangeSet** is a diff of Specs and is itself a bean. Because the factory's own structure is material, the same checks can protect maintenance work. This is how the foreman is kept in order.

## 2. Types, facts and properties

A type declares *facts*, which validators measure, and *properties*, which are named MRL expressions over those facts.

```yaml
# spec/examples/registry/types/video.yaml (excerpt)
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

Each validator ships with accept and reject fixtures. A change that makes a reject fixture pass, which would let bad material through, is classified as high risk automatically.

## 3. Consumer contracts: `guarantees` and `requires`

Ports state contracts in terms of properties. An output port lists what it **guarantees**, and the gate enforces it. An input port lists what it **requires**.

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

At compile time, every edge must satisfy `requires ⊆ guarantees ∪ type properties`. This is a set inclusion, so when an upstream station drops a guarantee, the compiler names exactly which downstream ports break. At run time, the intake check evaluates the same properties on the real bean.

## 4. Conversion: products become material automatically

When an edge's types or properties don't match, the compiler searches the registry for **converter** stations and inserts exactly one chain. When several chains qualify, it picks by these rules in order: lossless first, then fewest steps, then lowest estimated cost, then shortest estimated time, then name. You saw this in [Getting started](getting-started.md): `note.raw@1 → note.text@1` was bridged by `note-to-text@1`. When no chain exists, the compiler does not guess. It reports `E-NO-CONVERTER`, and a *conversion request* wakes the foreman, who can propose a new converter.

## 5. Lines

```yaml
# spec/examples/registry/lines/explainer-multilang.yaml (excerpt)
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

The rules:
- Every terminal product must be consumed by a sink or listed in `keep`.
- Budgets are hierarchical: global → line → run → station → job.
- Triggers can be `bean` arrivals, `cron` or `every` schedules, or manual.

### Bounded dynamic structure

| Construct | What it does | Bound |
|---|---|---|
| `map` | runs a sub-step for each item of a `list<T>` | `max_items` (reject or truncate on overflow), `max_parallel` |
| `switch` | picks one branch by an MRL condition | must be exhaustive, with a `default` |
| `until` | repeats a sub-step until an MRL condition holds | `max_iter`; exhausting it fails the run |
| `subline` | runs another line inside this run's epoch | depth limit |
| `plan` | a **planner** station emits a `Plan` (a sub-DAG) | compiled with V1/V2 and checked against `plan_bounds` before anything runs |

## 6. Epochs: change without breaking running work

Every registry commit that passes verification can become an **epoch**. A run binds to one epoch when it starts and keeps it until it finishes, so changing the structure never affects running work. Rolling back means moving the current-epoch pointer back. Several epochs can be live at once, for example the active epoch next to a canary.

## 7. Six verification layers

| Layer | What is checked | When | On failure |
|---|---|---|---|
| **V1** Spec | schema, references, pinned versions, policy lint | every proposal | reject the change |
| **V2** Structure | edge types and contracts, cycles, required inputs, consumed outputs, permissions, budget, dynamic bounds | proposal + before every run | reject change / run |
| **V3** Evolution | compatibility with the previous version, all downstream consumer contracts, blast radius | proposal | reject, or require a migration converter |
| **V4** Contract tests | machine conformance, golden briefs, converter round trips, sink dry runs | proposal + scheduled canaries | reject, or raise a drift alert |
| **V5** Runtime | intake check, gate (structure → validator → accept → judge), sink receipt verification | every job | reject → retry, or DLQ |
| **V6** Continuous | invariant audit, drift monitoring, CAS and lineage integrity, reconciliation | periodic + events | quarantine, circuit-break, roll back, wake the foreman |

## 8. Four kinds of drift

| Drift | Example | Caught by |
|---|---|---|
| Spec drift | someone edits a type or a port | V1–V3 and consumer contracts |
| Behaviour drift | the product upgrades, the model or prompt changes | pinned versions, V4 canaries, shadow runs |
| Semantic drift | output still passes the schema, but quality slides | acceptance expressions, sampled judges, metric baselines |
| Config drift | containers or secret bindings edited out of band | the reconciler and the invariant audit |

## 9. Governance tiers

The verification pipeline derives a risk tier from each ChangeSet's diff, deterministically. Nobody can lower it, the foreman included.

| Tier | Typical change | Requirements |
|---|---|---|
| T0 | retry, quarantine | automatic |
| T1 | template tuning, new converter, minor type bump | verification + shadow run |
| T2 | onboard or upgrade a machine, new line, major type bump | + canary + cross-review by a different runtime |
| T3 | new source/sink, egress or credential binding, policy change, retirement | + human approval |
| T4 | the constitution | humans only |

The constitution (L0) is the final anchor. Machines and agents can reshape everything else, but only through verified, reviewable, reversible ChangeSets.
