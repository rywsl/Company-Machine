**English** | [简体中文](../zh-CN/cli.md) | [日本語](../ja/cli.md)

# CLI Reference

There are two entry points, both installed by `uv sync`:

| Command | Package | Purpose |
|---|---|---|
| `factory` | `mao.cli:main` | the factory control plane |
| `mrl` | `mrl.cli:main` | the MRL (MAO Relation Language) toolkit: evaluate, check, explain diagnostics |

Inside sandboxes, operators use a third tool, `factoryctl`, described in [Runtime § 3](runtime.md#3-inside-a-station).

Run any command with `--help` for its exact arguments. The normative command list is [`spec/system/commands.md`](../../spec/system/commands.md).

## Global options

| Option | Env var | Default | Meaning |
|---|---|---|---|
| `--root DIR` | `FACTORY_ROOT` | `.factory` | factory state directory |
| `--registry DIR` | `FACTORY_REGISTRY` | `registry` | registry (Specs) directory |
| `--provider docker\|local` | | `docker` | sandbox provider |
| `--local-commands FILE` | | | command-name → host-command map for the local provider |
| `--epoch COMMIT` | | active epoch | run against a specific epoch |
| `--json` | | | machine-readable output |
| `--version` | | | print the version |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | business failure: job `DEAD`, run `FAILED`/`CANCELLED`, contract verdict `fail`, open audit findings |
| 2 | usage error |
| 3 | invalid registry (`REGISTRY_INVALID`) |
| 4 | environment or facility error, such as Docker being unavailable |
| 5 | internal error |

## Structure and governance

| Command | Description |
|---|---|
| `check ROOT` | load and compile a registry; exit 3 on errors |
| `epoch init COMMIT` · `ls` · `show [COMMIT]` · `rollback [COMMIT]` · `adopt COMMIT` | manage epochs and the active pointer |
| `changeset propose FILE --commit C --by ACTOR` · `ls [--state S]` · `show ID` · `withdraw ID --by ACTOR` · `advance ID` | the ChangeSet pipeline |
| `governance tick` · `governance safe-mode` | advance ChangeSets once; show or evaluate safe mode |
| `contract validator …` · `golden …` · `equivalence …` · `diff …` · `sink-dry-run …` · `ls` | V4 contract tests |
| `audit once` · `audit ls [--state S]` | run the invariant audit / list findings |
| `drift ls` | drift findings |

## Material

| Command | Description |
|---|---|
| `bean import --type TYPE --file PATH=HOST …` | admit external files as a bean; prints its id |
| `bean lineage BEAN --direction up\|down` | jobs and beans upstream or downstream |
| `bean label BEAN KEY=VALUE` | add a label (labels can trigger lines) |
| `cache ls [--station S@N]` · `explain JOB` · `invalidate (--key\|--job\|--station\|--bean) … --reason TEXT` | the job cache |
| `gc [--dry-run]` | retention: tombstones, blob sweep, workspace reclamation |

## Execution

| Command | Description |
|---|---|
| `job run STATION --input PORT=BEAN …` | submit and run one job inline (exit 0 sealed, 1 dead) |
| `job show JOB` · `job exec JOB` | inspect a job; take the lease on a submitted job and execute it (serve's worker subprocesses use this) |
| `line plan LINE@N` | compile a line into its run plan (nodes, converters, budgets) |
| `line run LINE@N --in PORT=BEAN … [--budget USD] [--variant STEP=V] [--no-cache]` | start a run and drive it to completion |
| `line ls` · `show RUN` · `nodes RUN` · `cancel RUN` · `retry RUN` · `rebuild RUN` | inspect and control runs |
| `trigger process` | evaluate line triggers once |
| `schedule tick` · `dlq ls` · `quarantine BEAN --reason TEXT` · `release BEAN` | scheduler, dead letters, quarantine |
| `approval ls [--job J] [--status S]` · `show ID` · `decide ID approve\|deny --by NAME [--note TEXT]` | approval points |

## Integration

| Command | Description |
|---|---|
| `image build …` · `image ls` | build and list machine images (foreman-built Dockerfiles) |
| `gateway manifest MACHINE@V` | the service gateway manifest for a form-B machine |
| `egress check STATION@N` | show a station's effective egress plan |
| `secret ls` | secret names and classes (never values) |
| `backend status` · `migrate …` · `verify` | storage backend: local ↔ S3 + Postgres |
| `backup create` · `verify` · `restore` | backups of state and blobs |

## Autonomy

| Command | Description |
|---|---|
| `serve [--once]` | the daemon: triggers, runs, jobs, governance, audit, canaries, wake tickets |
| `foreman next` · `done WAKE` · `get KEY` · `list KIND` · `diff BASE COMMIT` · `bean …` · `job …` · `dlq` · `drift` · `invariant` · `metrics` · `approval` · `changeset` · `review` · `conversion` · `supervise` · `sessions` · `wakes` · `reopen` | the foreman's operating surface |
| `mcp --role foreman\|reviewer` | the same surface as an MCP server on stdio |
| `maintain bootstrap` | check that the maintenance lines and `registry-sink` exist; exit 1 and list what is missing |

## Observability

| Command | Description |
|---|---|
| `status` | one JSON snapshot of the whole factory |
| `metrics` | Prometheus text format |
| `events [--since SEQ] [--kind K] [--limit N]` | the event log |
| `console [--port N]` | local read-only web console (default `127.0.0.1:8090`) |

## `mrl`

| Command | Description |
|---|---|
| `mrl eval EXPR` · `mrl template TEXT` | evaluate an expression / render a template |
| `mrl check MODULE` · `mrl run MODULE` | statically check / evaluate a rules module |
| `mrl registry …` | registry checks and queries (`mrl registry check --strict`) |
| `mrl resolve …` | resolve conversions for a hypothetical edge |
| `mrl explain CODE` | explain a diagnostic code such as `E-NO-CONVERTER` |
| `mrl conformance` · `mrl coverage` | run the conformance suite / show coverage of factory cases |
