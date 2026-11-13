[English](../en/cli.md) | [日本語](../ja/cli.md) | [한국어](../ko/cli.md) | [Français](../fr/cli.md) | [Deutsch](../de/cli.md) | [Español](../es/cli.md) | **Português (BR)** | [Русский](../ru/cli.md) | [简体中文](../zh-CN/cli.md)

# Referência da CLI

Há dois pontos de entrada, ambos instalados pelo `uv sync`:

| Comando | Pacote | Finalidade |
|---|---|---|
| `factory` | `mao.cli:main` | o plano de controle da fábrica |
| `mrl` | `mrl.cli:main` | o kit de ferramentas da MRL (MAO Relation Language): avaliar, verificar, explicar diagnósticos |

Dentro dos sandboxes, os operadores usam uma terceira ferramenta, o `factoryctl`, descrito em [Runtime § 3](runtime.md#3-dentro-de-uma-estação).

Rode qualquer comando com `--help` para ver os argumentos exatos. A lista normativa de comandos é [`spec/system/commands.md`](../../spec/system/commands.md).

## Opções globais

| Opção | Variável de ambiente | Padrão | Significado |
|---|---|---|---|
| `--root DIR` | `FACTORY_ROOT` | `.factory` | diretório de estado da fábrica |
| `--registry DIR` | `FACTORY_REGISTRY` | `registry` | diretório do registro (Specs) |
| `--provider docker\|local` | | `docker` | provedor de sandbox |
| `--local-commands FILE` | | | mapa nome de comando → comando do host para o provedor local |
| `--epoch COMMIT` | | epoch ativa | executar contra uma epoch específica |
| `--json` | | | saída legível por máquina |
| `--version` | | | imprimir a versão |

## Códigos de saída

| Código | Significado |
|---|---|
| 0 | sucesso |
| 1 | falha de negócio: job `DEAD`, execução `FAILED`/`CANCELLED`, veredito de contrato `fail`, achados de auditoria em aberto |
| 2 | erro de uso |
| 3 | registro inválido (`REGISTRY_INVALID`) |
| 4 | erro de ambiente ou de infraestrutura, como Docker indisponível |
| 5 | erro interno |

## Estrutura e governança

| Comando | Descrição |
|---|---|
| `check ROOT` | carregar e compilar um registro; código 3 em caso de erros |
| `epoch init COMMIT` · `ls` · `show [COMMIT]` · `rollback [COMMIT]` · `adopt COMMIT` | gerenciar as epochs e o ponteiro ativo |
| `changeset propose FILE --commit C --by ACTOR` · `ls [--state S]` · `show ID` · `withdraw ID --by ACTOR` · `advance ID` | o pipeline de ChangeSets |
| `governance tick` · `governance safe-mode` | avançar os ChangeSets uma vez; mostrar ou avaliar o modo seguro |
| `contract validator …` · `golden …` · `equivalence …` · `diff …` · `sink-dry-run …` · `ls` | testes de contrato V4 |
| `audit once` · `audit ls [--state S]` | rodar a auditoria de invariantes / listar achados |
| `drift ls` | achados de deriva |

## Material

| Comando | Descrição |
|---|---|
| `bean import --type TYPE --file PATH=HOST …` | admitir arquivos externos como um bean; imprime o id dele |
| `bean lineage BEAN --direction up\|down` | jobs e beans a montante ou a jusante |
| `bean label BEAN KEY=VALUE` | adicionar um rótulo (rótulos podem disparar linhas) |
| `cache ls [--station S@N]` · `explain JOB` · `invalidate (--key\|--job\|--station\|--bean) … --reason TEXT` | o cache de jobs |
| `gc [--dry-run]` | retenção: tombstones, varredura de blobs, recuperação de espaços de trabalho |

## Execução

| Comando | Descrição |
|---|---|
| `job run STATION --input PORT=BEAN …` | submeter e executar um job inline (código 0 selado, 1 morto) |
| `job show JOB` · `job exec JOB` | inspecionar um job; assumir o lease de um job submetido e executá-lo (usado pelos subprocessos worker do serve) |
| `line plan LINE@N` | compilar uma linha em seu plano de execução (nós, conversores, orçamentos) |
| `line run LINE@N --in PORT=BEAN … [--budget USD] [--variant STEP=V] [--no-cache]` | iniciar uma execução e conduzi-la até o fim |
| `line ls` · `show RUN` · `nodes RUN` · `cancel RUN` · `retry RUN` · `rebuild RUN` | inspecionar e controlar execuções |
| `trigger process` | avaliar os gatilhos das linhas uma vez |
| `schedule tick` · `dlq ls` · `quarantine BEAN --reason TEXT` · `release BEAN` | agendador, mensagens mortas, quarentena |
| `approval ls [--job J] [--status S]` · `show ID` · `decide ID approve\|deny --by NAME [--note TEXT]` | pontos de aprovação |

## Integração

| Comando | Descrição |
|---|---|
| `image build …` · `image ls` | construir e listar imagens de máquinas (Dockerfiles construídos pelo Foreman) |
| `gateway manifest MACHINE@V` | o manifesto do gateway de serviços para uma máquina de forma B |
| `egress check STATION@N` | mostrar o plano de saída de rede efetivo de uma estação |
| `secret ls` | nomes e classes dos segredos (nunca os valores) |
| `backend status` · `migrate …` · `verify` | backend de armazenamento: local ↔ S3 + Postgres |
| `backup create` · `verify` · `restore` | backups do estado e dos blobs |

## Autonomia

| Comando | Descrição |
|---|---|
| `serve [--once]` | o daemon: gatilhos, execuções, jobs, governança, auditoria, canários, tickets de despertar |
| `foreman next` · `done WAKE` · `get KEY` · `list KIND` · `diff BASE COMMIT` · `bean …` · `job …` · `dlq` · `drift` · `invariant` · `metrics` · `approval` · `changeset` · `review` · `conversion` · `supervise` · `sessions` · `wakes` · `reopen` | a superfície de trabalho do Foreman (capataz) |
| `mcp --role foreman\|reviewer` | a mesma superfície como servidor MCP via stdio |
| `maintain bootstrap` | verificar se as linhas de manutenção e o `registry-sink` existem; caso contrário, código 1 e a lista do que falta |

## Observabilidade

| Comando | Descrição |
|---|---|
| `status` | um snapshot JSON da fábrica inteira |
| `metrics` | formato de texto do Prometheus |
| `events [--since SEQ] [--kind K] [--limit N]` | o log de eventos |
| `console [--port N]` | console web local somente leitura (padrão `127.0.0.1:8090`) |

## `mrl`

| Comando | Descrição |
|---|---|
| `mrl eval EXPR` · `mrl template TEXT` | avaliar uma expressão / renderizar um template |
| `mrl check MODULE` · `mrl run MODULE` | verificar estaticamente / avaliar um módulo de regras |
| `mrl registry …` | verificações e consultas no registro (`mrl registry check --strict`) |
| `mrl resolve …` | resolver conversões para uma aresta hipotética |
| `mrl explain CODE` | explicar um código de diagnóstico como `E-NO-CONVERTER` |
| `mrl conformance` · `mrl coverage` | rodar a suíte de conformidade / mostrar a cobertura dos casos de fábrica |
