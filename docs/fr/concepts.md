[English](../en/concepts.md) | [日本語](../ja/concepts.md) | [한국어](../ko/concepts.md) | **Français** | [Deutsch](../de/concepts.md) | [Español](../es/concepts.md) | [Português (BR)](../pt-BR/concepts.md) | [Русский](../ru/concepts.md) | [简体中文](../zh-CN/concepts.md)

# Concepts

Machine Agent Org repose sur un méta-modèle réduit. Tout ce que vous pouvez construire (un pipeline vidéo, une ligne de revue de code, un rapport nocturne) s'exprime avec les sept primitives ci-dessous, écrites sous forme de **Specs** YAML dans un registre Git. Le compilateur de structure vérifie ces Specs comme un vérificateur de types vérifie du code.

> Justification de la conception : sections 3 à 10 de [`docs/architecture.md`](../architecture.md) (en chinois). Règles formelles : [`spec/mrl/factory-profile.md`](../../spec/mrl/factory-profile.md).

## 1. Les primitives

| Primitive | Signification | Points clés |
|---|---|---|
| **Type** | la spécification d'un matériau | `name@major.minor`. Genres : atomique, `bundle{…}`, `list<T>`, `ref<T>`, `any`. Porte une disposition de charge utile, un schéma de métadonnées, des validateurs (avec des fixtures d'acceptation et de rejet), des *faits* mesurés et des *propriétés* nommées |
| **Bean** (matériau) | un élément de matériau | adressé par son contenu (`sha256:…`), immuable, scellé avec son lignage et sa provenance |
| **Machine** | un produit existant | décrite par un Machine Package : image épinglée par digest, recettes, entrées et sorties, sondes, références de conformité |
| **Operator** (opérateur) | celui qui pilote la machine | un agent CLI (Claude Code headless, Codex), un script, ou un humain via une boîte de réception |
| **Station** | Recipe × Operator × Policy | ports typés. Rôles : `source`, `transform`, `sink` ; `converter`, `judge` et `planner` sont des transformations spécialisées |
| **Line** (ligne) | un DAG typé de stations | plus des constructions dynamiques bornées. Une ligne dotée d'une interface peut servir de station (`subline`) |
| **Policy** (politique) | les permissions | niveau de sortie réseau, secrets, budget, approbations, publication, rétention |

Trois concepts de niveau méta s'ajoutent par-dessus. Une **Spec** est n'importe laquelle des définitions ci-dessus, stockée comme un bean de type `spec.<kind>@N`. Un **Invariant** est une règle de la constitution écrite en MRL. Un **ChangeSet** est un diff de Specs, et c'est lui-même un bean. Comme la structure même de l'usine est du matériau, les mêmes contrôles peuvent protéger le travail de maintenance. C'est ainsi que le Foreman (contremaître) est tenu en ordre.

## 2. Types, faits et propriétés

Un type déclare des *faits*, que les validateurs mesurent, et des *propriétés*, qui sont des expressions MRL nommées portant sur ces faits.

```yaml
# spec/examples/registry/types/video.yaml (extrait)
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

Chaque validateur est livré avec des fixtures d'acceptation et de rejet. Un changement qui ferait passer une fixture de rejet, et laisserait donc passer du mauvais matériau, est automatiquement classé à haut risque.

## 3. Contrats consommateurs : `guarantees` et `requires`

Les ports expriment leurs contrats en termes de propriétés. Un port de sortie liste ce qu'il **garantit** (`guarantees`), et le gate (contrôle de sortie) le fait respecter. Un port d'entrée liste ce qu'il **exige** (`requires`).

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

À la compilation, chaque arête doit satisfaire `requires ⊆ guarantees ∪ type properties`. C'est une inclusion d'ensembles : quand une station amont abandonne une garantie, le compilateur indique précisément quels ports aval sont cassés. À l'exécution, le contrôle d'entrée évalue les mêmes propriétés sur le bean réel.

## 4. Conversion : les produits deviennent automatiquement du matériau

Quand les types ou les propriétés d'une arête ne correspondent pas, le compilateur cherche dans le registre des stations de **conversion** (`converter`) et insère exactement une chaîne. Si plusieurs chaînes conviennent, il choisit selon ces règles, dans l'ordre : sans perte d'abord, puis le moins d'étapes, puis le coût estimé le plus bas, puis la durée estimée la plus courte, puis le nom. Vous l'avez vu dans [Prise en main](getting-started.md) : `note.raw@1 → note.text@1` a été relié par `note-to-text@1`. Quand aucune chaîne n'existe, le compilateur ne devine pas. Il signale `E-NO-CONVERTER`, et une *demande de conversion* réveille le Foreman, qui peut proposer un nouveau convertisseur.

## 5. Lignes

```yaml
# spec/examples/registry/lines/explainer-multilang.yaml (extrait)
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

Les règles :
- Chaque produit terminal doit être consommé par un sink (station de publication) ou figurer dans `keep`.
- Les budgets sont hiérarchiques : global → ligne → exécution → station → job.
- Les déclencheurs peuvent être des arrivées de `bean`, des planifications `cron` ou `every`, ou un lancement manuel.

### Structure dynamique bornée

| Construction | Ce qu'elle fait | Borne |
|---|---|---|
| `map` | exécute une sous-étape pour chaque élément d'une `list<T>` | `max_items` (rejet ou troncature en cas de dépassement), `max_parallel` |
| `switch` | choisit une branche selon une condition MRL | doit être exhaustif, avec un `default` |
| `until` | répète une sous-étape jusqu'à ce qu'une condition MRL soit vraie | `max_iter` ; l'épuiser fait échouer l'exécution |
| `subline` | exécute une autre ligne dans l'epoch de l'exécution en cours | limite de profondeur |
| `plan` | une station **planner** émet un `Plan` (un sous-DAG) | compilé avec V1/V2 et confronté aux `plan_bounds` avant que quoi que ce soit ne s'exécute |

## 6. Epochs : changer sans casser le travail en cours

Chaque commit du registre qui passe la vérification peut devenir une **epoch** (une version figée de la structure). Une exécution se lie à une epoch au démarrage et la conserve jusqu'à la fin : modifier la structure n'affecte donc jamais le travail en cours. Revenir en arrière consiste à replacer le pointeur d'epoch courante sur une version antérieure. Plusieurs epochs peuvent être en service en même temps, par exemple l'epoch active à côté d'un canari.

## 7. Six couches de vérification

| Couche | Ce qui est vérifié | Quand | En cas d'échec |
|---|---|---|---|
| **V1** Spec | schéma, références, versions épinglées, lint des politiques | à chaque proposition | rejet du changement |
| **V2** Structure | types et contrats des arêtes, cycles, entrées requises, sorties consommées, permissions, budget, bornes dynamiques | proposition + avant chaque exécution | rejet du changement / de l'exécution |
| **V3** Évolution | compatibilité avec la version précédente, tous les contrats des consommateurs en aval, rayon d'impact | proposition | rejet, ou convertisseur de migration exigé |
| **V4** Tests de contrat | conformité des machines, briefs de référence, allers-retours des convertisseurs, exécutions à blanc des sinks | proposition + canaris planifiés | rejet, ou alerte de dérive |
| **V5** Runtime | contrôle d'entrée, gate (structure → validateur → acceptation → juge), vérification des reçus de sink | chaque job | rejet → nouvelle tentative, ou DLQ |
| **V6** Continu | audit des invariants, surveillance de la dérive, intégrité du CAS et du lignage, réconciliation | périodique + événements | quarantaine, coupe-circuit, rollback, réveil du Foreman |

## 8. Quatre types de dérive

| Dérive | Exemple | Détectée par |
|---|---|---|
| Dérive de spec | quelqu'un modifie un type ou un port | V1–V3 et les contrats consommateurs |
| Dérive de comportement | le produit est mis à jour, le modèle ou le prompt change | versions épinglées, canaris V4, exécutions shadow |
| Dérive sémantique | la sortie respecte toujours le schéma, mais la qualité se dégrade | expressions d'acceptation, juges par échantillonnage, métriques de référence |
| Dérive de configuration | conteneurs ou liaisons de secrets modifiés hors procédure | le réconciliateur et l'audit des invariants |

## 9. Niveaux de gouvernance

Le pipeline de vérification déduit de façon déterministe un niveau de risque à partir du diff de chaque ChangeSet. Personne ne peut l'abaisser, pas même le Foreman.

| Niveau | Changement typique | Exigences |
|---|---|---|
| T0 | nouvelle tentative, quarantaine | automatique |
| T1 | réglage de template, nouveau convertisseur, montée de version mineure d'un type | vérification + exécution shadow |
| T2 | intégrer ou mettre à niveau une machine, nouvelle ligne, montée de version majeure d'un type | + canari + revue croisée par un runtime différent |
| T3 | nouvelle source ou nouveau sink, sortie réseau ou liaison d'identifiants, changement de politique, retrait | + approbation humaine |
| T4 | la constitution | humains uniquement |

La constitution (L0) est l'ancrage ultime. Machines et agents peuvent remodeler tout le reste, mais uniquement par des ChangeSets vérifiés, relisibles et réversibles.
