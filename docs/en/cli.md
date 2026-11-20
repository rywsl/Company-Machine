**English** | [简体中文](../zh-CN/cli.md)

# CLI reference

The active command-line tools run on Bun. The control-plane CLI is `factory`
and the MRL toolkit is `mrl`. Historical Python commands and their complete
reference remain under `removed/python/` for migration review.

## Install and entry points

```bash
bun install
bun run factory --help
bun run mrl --help
```

The Web API can run in the foreground with `bun run factory serve`.

## `factory`

| Command | Purpose |
|---|---|
| `status` | Print the local business-system and Foreman queue summary. |
| `serve` | Start the Bun Web API and Web App on `127.0.0.1:3000`. |
| `runtime status` | Read the selected Podman or Kiri health state. |
| `runtime ensure-ready` | Ask the selected runtime to become ready. Exit `4` when it remains unavailable. |
| `project intake --name NAME --repo URL` | Create a project from a repository URL. |
| `project intake --name NAME --folder PATH` | Create a project from a local folder reference. |
| `project intake --name NAME --file-ref REF` | Create a project from an uploaded file reference. |
| `business units ID` | List the business-system Operator and Machine units. |
| `business run ID [--input REF]` | Queue a business run and return its unit runs. |
| `business runs ID` | List business runs and unit-run status. |
| `business export ID` | Export the portable business-system document. |
| `business graph ID --file PATH` | Submit a Foreman-maintained DAG document. |
| `business artifact PROJECT_ID --file PATH` | Record a human-readable artifact or material document. |
| `business conversion ID --kind KIND --summary TEXT` | Record an artifact-to-material conversion. |
| `foreman inbox [--limit N]` | List queued Foreman messages. |
| `foreman claim ID` / `foreman done ID` | Claim or complete a Foreman message. |
| `talk send TEXT` | Queue a human message for the configured official Harness. |
| `talk reply TEXT --turn-id ID` | Append a Foreman reply to a talk thread. |

Business records are JSON at the API boundary. The Web App presents them as
graphs, timelines and material lineage views.

## `mrl`

The Bun MRL CLI accepts JSON mappings inline or from `@FILE` references.

| Command | Purpose |
|---|---|
| `mrl eval EXPR [--env JSON|@FILE] [--types JSON|@FILE] [--budget N] [--json]` | Statically check and evaluate an expression. |
| `mrl template TEXT [--env JSON|@FILE] [--types JSON|@FILE] [--budget N] [--json]` | Render a checked template. |
| `mrl check FILE... [--json]` | Parse and validate MRL rule modules. |
| `mrl run FILE... [--facts JSON|@FILE] [--rel NAME]... [--json]` | Evaluate rule modules against input facts. |

Exit codes are `0` for success, `2` for usage errors, `3` for static MRL
errors, and `4` for evaluation or rule-runtime errors.

## Historical reference

The former Python `factory`, `mrl`, and `factoryctl` implementations are
preserved in [`removed/python/`](../../removed/python/). They are not loaded by
the Bun control plane or by `bun run verify:bun`.
