[English](../en/cli.md) | [日本語](../ja/cli.md) | [한국어](../ko/cli.md) | **Français** | [Deutsch](../de/cli.md) | [Español](../es/cli.md) | [Português (BR)](../pt-BR/cli.md) | [Русский](../ru/cli.md) | [简体中文](../zh-CN/cli.md)

# Référence de la CLI

Il y a deux points d'entrée, tous deux installés par `uv sync` :

| Commande | Paquet | Rôle |
|---|---|---|
| `factory` | `mao.cli:main` | le plan de contrôle de l'usine |
| `mrl` | `mrl.cli:main` | la boîte à outils MRL (MAO Relation Language) : évaluer, vérifier, expliquer les diagnostics |

Dans les sandboxes, les opérateurs utilisent un troisième outil, `factoryctl`, décrit dans [Runtime § 3](runtime.md#3-à-lintérieur-dune-station).

Lancez n'importe quelle commande avec `--help` pour connaître ses arguments exacts. La liste normative des commandes est [`spec/system/commands.md`](../../spec/system/commands.md).

## Options globales

| Option | Variable d'env. | Défaut | Signification |
|---|---|---|---|
| `--root DIR` | `FACTORY_ROOT` | `.factory` | répertoire d'état de l'usine |
| `--registry DIR` | `FACTORY_REGISTRY` | `registry` | répertoire du registre (Specs) |
| `--provider docker\|local` | | `docker` | fournisseur de sandbox |
| `--local-commands FILE` | | | table nom de commande → commande de l'hôte pour le fournisseur local |
| `--epoch COMMIT` | | epoch active | exécuter sur une epoch précise |
| `--json` | | | sortie lisible par une machine |
| `--version` | | | afficher la version |

## Codes de sortie

| Code | Signification |
|---|---|
| 0 | succès |
| 1 | échec métier : job `DEAD`, exécution `FAILED`/`CANCELLED`, verdict de contrat `fail`, constats d'audit ouverts |
| 2 | erreur d'utilisation |
| 3 | registre invalide (`REGISTRY_INVALID`) |
| 4 | erreur d'environnement ou d'infrastructure, par exemple Docker indisponible |
| 5 | erreur interne |

## Structure et gouvernance

| Commande | Description |
|---|---|
| `check ROOT` | charger et compiler un registre ; code 3 en cas d'erreurs |
| `epoch init COMMIT` · `ls` · `show [COMMIT]` · `rollback [COMMIT]` · `adopt COMMIT` | gérer les epochs et le pointeur actif |
| `changeset propose FILE --commit C --by ACTOR` · `ls [--state S]` · `show ID` · `withdraw ID --by ACTOR` · `advance ID` | le pipeline des ChangeSets |
| `governance tick` · `governance safe-mode` | faire avancer les ChangeSets une fois ; afficher ou évaluer le mode sûr |
| `contract validator …` · `golden …` · `equivalence …` · `diff …` · `sink-dry-run …` · `ls` | tests de contrat V4 |
| `audit once` · `audit ls [--state S]` | lancer l'audit des invariants / lister les constats |
| `drift ls` | constats de dérive |

## Matériaux

| Commande | Description |
|---|---|
| `bean import --type TYPE --file PATH=HOST …` | admettre des fichiers externes comme bean ; affiche son id |
| `bean lineage BEAN --direction up\|down` | jobs et beans en amont ou en aval |
| `bean label BEAN KEY=VALUE` | ajouter un label (les labels peuvent déclencher des lignes) |
| `cache ls [--station S@N]` · `explain JOB` · `invalidate (--key\|--job\|--station\|--bean) … --reason TEXT` | le cache des jobs |
| `gc [--dry-run]` | rétention : tombstones, nettoyage des blobs, récupération des espaces de travail |

## Exécution

| Commande | Description |
|---|---|
| `job run STATION --input PORT=BEAN …` | soumettre et exécuter un job en ligne (code 0 scellé, 1 mort) |
| `job show JOB` · `job exec JOB` | inspecter un job ; prendre le bail d'un job soumis et l'exécuter (utilisé par les sous-processus workers de serve) |
| `line plan LINE@N` | compiler une ligne en plan d'exécution (nœuds, convertisseurs, budgets) |
| `line run LINE@N --in PORT=BEAN … [--budget USD] [--variant STEP=V] [--no-cache]` | démarrer une exécution et la mener à son terme |
| `line ls` · `show RUN` · `nodes RUN` · `cancel RUN` · `retry RUN` · `rebuild RUN` | inspecter et piloter les exécutions |
| `trigger process` | évaluer une fois les déclencheurs de lignes |
| `schedule tick` · `dlq ls` · `quarantine BEAN --reason TEXT` · `release BEAN` | ordonnanceur, lettres mortes, quarantaine |
| `approval ls [--job J] [--status S]` · `show ID` · `decide ID approve\|deny --by NAME [--note TEXT]` | points d'approbation |

## Intégration

| Commande | Description |
|---|---|
| `image build …` · `image ls` | construire et lister les images de machines (Dockerfiles construits par le Foreman) |
| `gateway manifest MACHINE@V` | le manifeste de la passerelle de services pour une machine de forme B |
| `egress check STATION@N` | afficher le plan de sortie réseau effectif d'une station |
| `secret ls` | noms et classes des secrets (jamais les valeurs) |
| `backend status` · `migrate …` · `verify` | backend de stockage : local ↔ S3 + Postgres |
| `backup create` · `verify` · `restore` | sauvegardes de l'état et des blobs |

## Autonomie

| Commande | Description |
|---|---|
| `serve [--once]` | le démon : déclencheurs, exécutions, jobs, gouvernance, audit, canaris, tickets de réveil |
| `foreman next` · `done WAKE` · `get KEY` · `list KIND` · `diff BASE COMMIT` · `bean …` · `job …` · `dlq` · `drift` · `invariant` · `metrics` · `approval` · `changeset` · `review` · `conversion` · `supervise` · `sessions` · `wakes` · `reopen` | la surface de travail du Foreman (contremaître) |
| `mcp --role foreman\|reviewer` | la même surface sous forme de serveur MCP sur stdio |
| `maintain bootstrap` | vérifier que les lignes de maintenance et `registry-sink` existent ; code 1 et liste de ce qui manque sinon |

## Observabilité

| Commande | Description |
|---|---|
| `status` | un instantané JSON de toute l'usine |
| `metrics` | format texte Prometheus |
| `events [--since SEQ] [--kind K] [--limit N]` | le journal d'événements |
| `console [--port N]` | console web locale en lecture seule (par défaut `127.0.0.1:8090`) |

## `mrl`

| Commande | Description |
|---|---|
| `mrl eval EXPR` · `mrl template TEXT` | évaluer une expression / rendre un template |
| `mrl check MODULE` · `mrl run MODULE` | vérifier statiquement / évaluer un module de règles |
| `mrl registry …` | vérifications et requêtes sur le registre (`mrl registry check --strict`) |
| `mrl resolve …` | résoudre les conversions pour une arête hypothétique |
| `mrl explain CODE` | expliquer un code de diagnostic comme `E-NO-CONVERTER` |
| `mrl conformance` · `mrl coverage` | lancer la suite de conformité / afficher la couverture des cas de l'usine |
