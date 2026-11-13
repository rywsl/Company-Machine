[English](../en/cli.md) | [日本語](../ja/cli.md) | [한국어](../ko/cli.md) | [Français](../fr/cli.md) | [Deutsch](../de/cli.md) | [Español](../es/cli.md) | [Português (BR)](../pt-BR/cli.md) | **Русский** | [简体中文](../zh-CN/cli.md)

# Справочник CLI

Есть две точки входа, обе устанавливаются командой `uv sync`:

| Команда | Пакет | Назначение |
|---|---|---|
| `factory` | `mao.cli:main` | плоскость управления фабрикой |
| `mrl` | `mrl.cli:main` | инструментарий MRL (MAO Relation Language): вычисление, проверка, объяснение диагностик |

Внутри песочниц операторы используют третий инструмент, `factoryctl`, описанный в разделе [Среда выполнения § 3](runtime.md#3-внутри-станции).

Запустите любую команду с `--help`, чтобы увидеть её точные аргументы. Нормативный список команд — [`spec/system/commands.md`](../../spec/system/commands.md).

## Глобальные параметры

| Параметр | Переменная окружения | По умолчанию | Значение |
|---|---|---|---|
| `--root DIR` | `FACTORY_ROOT` | `.factory` | каталог состояния фабрики |
| `--registry DIR` | `FACTORY_REGISTRY` | `registry` | каталог реестра (Spec) |
| `--provider docker\|local` | | `docker` | провайдер песочниц |
| `--local-commands FILE` | | | таблица «имя команды → команда хоста» для локального провайдера |
| `--epoch COMMIT` | | активная эпоха | выполнить на конкретной эпохе |
| `--json` | | | машиночитаемый вывод |
| `--version` | | | вывести версию |

## Коды выхода

| Код | Значение |
|---|---|
| 0 | успех |
| 1 | прикладная ошибка: задание `DEAD`, запуск `FAILED`/`CANCELLED`, вердикт контракта `fail`, открытые замечания аудита |
| 2 | неверное использование |
| 3 | некорректный реестр (`REGISTRY_INVALID`) |
| 4 | ошибка окружения или инфраструктуры, например недоступен Docker |
| 5 | внутренняя ошибка |

## Структура и управление

| Команда | Описание |
|---|---|
| `check ROOT` | загрузить и скомпилировать реестр; при ошибках код 3 |
| `epoch init COMMIT` · `ls` · `show [COMMIT]` · `rollback [COMMIT]` · `adopt COMMIT` | управление эпохами и указателем активной эпохи |
| `changeset propose FILE --commit C --by ACTOR` · `ls [--state S]` · `show ID` · `withdraw ID --by ACTOR` · `advance ID` | конвейер ChangeSet |
| `governance tick` · `governance safe-mode` | однократно продвинуть ChangeSet; показать или оценить безопасный режим |
| `contract validator …` · `golden …` · `equivalence …` · `diff …` · `sink-dry-run …` · `ls` | контрактные тесты V4 |
| `audit once` · `audit ls [--state S]` | запустить аудит инвариантов / показать замечания |
| `drift ls` | замечания о дрейфе |

## Материал

| Команда | Описание |
|---|---|
| `bean import --type TYPE --file PATH=HOST …` | принять внешние файлы как Bean; выводит его id |
| `bean lineage BEAN --direction up\|down` | вышестоящие или нижестоящие задания и Bean |
| `bean label BEAN KEY=VALUE` | добавить метку (метки могут запускать линии) |
| `cache ls [--station S@N]` · `explain JOB` · `invalidate (--key\|--job\|--station\|--bean) … --reason TEXT` | кеш заданий |
| `gc [--dry-run]` | сроки хранения: tombstone-записи, очистка блобов, освобождение рабочих каталогов |

## Исполнение

| Команда | Описание |
|---|---|
| `job run STATION --input PORT=BEAN …` | отправить и выполнить одно задание прямо в процессе команды (код 0 — запечатано, 1 — DEAD) |
| `job show JOB` · `job exec JOB` | посмотреть задание; взять аренду отправленного задания и выполнить его (так работают рабочие подпроцессы serve) |
| `line plan LINE@N` | скомпилировать линию в план запуска (узлы, конвертеры, бюджеты) |
| `line run LINE@N --in PORT=BEAN … [--budget USD] [--variant STEP=V] [--no-cache]` | начать запуск и довести его до конца |
| `line ls` · `show RUN` · `nodes RUN` · `cancel RUN` · `retry RUN` · `rebuild RUN` | просмотр запусков и управление ими |
| `trigger process` | однократно вычислить триггеры линий |
| `schedule tick` · `dlq ls` · `quarantine BEAN --reason TEXT` · `release BEAN` | планировщик, мёртвые письма, карантин |
| `approval ls [--job J] [--status S]` · `show ID` · `decide ID approve\|deny --by NAME [--note TEXT]` | точки согласования |

## Интеграция

| Команда | Описание |
|---|---|
| `image build …` · `image ls` | собрать и перечислить образы машин (Dockerfile, написанные бригадиром) |
| `gateway manifest MACHINE@V` | манифест шлюза сервисов для машины формы B |
| `egress check STATION@N` | показать действующий план выхода в сеть для станции |
| `secret ls` | имена и классы секретов (но никогда не значения) |
| `backend status` · `migrate …` · `verify` | бэкенд хранения: локальный ↔ S3 + Postgres |
| `backup create` · `verify` · `restore` | резервные копии состояния и блобов |

## Автономность

| Команда | Описание |
|---|---|
| `serve [--once]` | демон: триггеры, запуски, задания, управление, аудит, канарейки, тикеты пробуждения |
| `foreman next` · `done WAKE` · `get KEY` · `list KIND` · `diff BASE COMMIT` · `bean …` · `job …` · `dlq` · `drift` · `invariant` · `metrics` · `approval` · `changeset` · `review` · `conversion` · `supervise` · `sessions` · `wakes` · `reopen` | рабочий интерфейс бригадира (Foreman) |
| `mcp --role foreman\|reviewer` | тот же интерфейс в виде MCP-сервера на stdio |
| `maintain bootstrap` | проверить, что существуют линии обслуживания и `registry-sink`; иначе код 1 и список недостающего |

## Наблюдаемость

| Команда | Описание |
|---|---|
| `status` | один JSON-снимок всей фабрики |
| `metrics` | текстовый формат Prometheus |
| `events [--since SEQ] [--kind K] [--limit N]` | журнал событий |
| `console [--port N]` | локальная веб-консоль только для чтения (по умолчанию `127.0.0.1:8090`) |

## `mrl`

| Команда | Описание |
|---|---|
| `mrl eval EXPR` · `mrl template TEXT` | вычислить выражение / отрисовать шаблон |
| `mrl check MODULE` · `mrl run MODULE` | статически проверить / выполнить модуль правил |
| `mrl registry …` | проверки реестра и запросы к нему (`mrl registry check --strict`) |
| `mrl resolve …` | подобрать конвертацию для гипотетического ребра |
| `mrl explain CODE` | объяснить код диагностики, например `E-NO-CONVERTER` |
| `mrl conformance` · `mrl coverage` | запустить набор тестов соответствия / показать покрытие заводских сценариев |
