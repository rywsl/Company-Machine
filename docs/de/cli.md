[English](../en/cli.md) | [日本語](../ja/cli.md) | [한국어](../ko/cli.md) | [Français](../fr/cli.md) | **Deutsch** | [Español](../es/cli.md) | [Português (BR)](../pt-BR/cli.md) | [Русский](../ru/cli.md) | [简体中文](../zh-CN/cli.md)

# CLI-Referenz

Es gibt zwei Einstiegspunkte, beide werden von `uv sync` installiert:

| Befehl | Paket | Zweck |
|---|---|---|
| `factory` | `mao.cli:main` | die Steuerungsebene der Fabrik |
| `mrl` | `mrl.cli:main` | das Toolkit für MRL (MAO Relation Language): auswerten, prüfen, Diagnosen erklären |

In Sandboxes verwenden Operatoren ein drittes Werkzeug, `factoryctl`, beschrieben in [Laufzeit § 3](runtime.md#3-im-inneren-einer-station).

Rufen Sie einen beliebigen Befehl mit `--help` auf, um seine genauen Argumente zu sehen. Die normative Befehlsliste ist [`spec/system/commands.md`](../../spec/system/commands.md).

## Globale Optionen

| Option | Umgebungsvariable | Standard | Bedeutung |
|---|---|---|---|
| `--root DIR` | `FACTORY_ROOT` | `.factory` | Zustandsverzeichnis der Fabrik |
| `--registry DIR` | `FACTORY_REGISTRY` | `registry` | Verzeichnis der Registry (Specs) |
| `--provider docker\|local` | | `docker` | Sandbox-Provider |
| `--local-commands FILE` | | | Zuordnung Befehlsname → Host-Befehl für den lokalen Provider |
| `--epoch COMMIT` | | aktive Epoch | gegen eine bestimmte Epoch ausführen |
| `--json` | | | maschinenlesbare Ausgabe |
| `--version` | | | Version ausgeben |

## Exit-Codes

| Code | Bedeutung |
|---|---|
| 0 | Erfolg |
| 1 | fachlicher Fehler: Job `DEAD`, Lauf `FAILED`/`CANCELLED`, Vertragsurteil `fail`, offene Audit-Befunde |
| 2 | Bedienfehler |
| 3 | ungültige Registry (`REGISTRY_INVALID`) |
| 4 | Umgebungs- oder Infrastrukturfehler, etwa wenn Docker nicht verfügbar ist |
| 5 | interner Fehler |

## Struktur und Governance

| Befehl | Beschreibung |
|---|---|
| `check ROOT` | eine Registry laden und kompilieren; Exit-Code 3 bei Fehlern |
| `epoch init COMMIT` · `ls` · `show [COMMIT]` · `rollback [COMMIT]` · `adopt COMMIT` | Epochs und den aktiven Zeiger verwalten |
| `changeset propose FILE --commit C --by ACTOR` · `ls [--state S]` · `show ID` · `withdraw ID --by ACTOR` · `advance ID` | die ChangeSet-Pipeline |
| `governance tick` · `governance safe-mode` | ChangeSets einmal voranbringen; Safe Mode anzeigen oder bewerten |
| `contract validator …` · `golden …` · `equivalence …` · `diff …` · `sink-dry-run …` · `ls` | V4-Vertragstests |
| `audit once` · `audit ls [--state S]` | Invarianten-Audit ausführen / Befunde auflisten |
| `drift ls` | Drift-Befunde |

## Material

| Befehl | Beschreibung |
|---|---|
| `bean import --type TYPE --file PATH=HOST …` | externe Dateien als Bean aufnehmen; gibt ihre ID aus |
| `bean lineage BEAN --direction up\|down` | vor- oder nachgelagerte Jobs und Beans |
| `bean label BEAN KEY=VALUE` | ein Label hinzufügen (Labels können Linien auslösen) |
| `cache ls [--station S@N]` · `explain JOB` · `invalidate (--key\|--job\|--station\|--bean) … --reason TEXT` | der Job-Cache |
| `gc [--dry-run]` | Aufbewahrung: Tombstones, Aufräumen von Blobs, Rückgewinnung von Arbeitsbereichen |

## Ausführung

| Befehl | Beschreibung |
|---|---|
| `job run STATION --input PORT=BEAN …` | einen Job einreichen und inline ausführen (Exit-Code 0 versiegelt, 1 tot) |
| `job show JOB` · `job exec JOB` | einen Job untersuchen; die Lease eines eingereichten Jobs übernehmen und ihn ausführen (so arbeiten die Worker-Unterprozesse von serve) |
| `line plan LINE@N` | eine Linie in ihren Ausführungsplan kompilieren (Knoten, Konverter, Budgets) |
| `line run LINE@N --in PORT=BEAN … [--budget USD] [--variant STEP=V] [--no-cache]` | einen Lauf starten und bis zum Abschluss treiben |
| `line ls` · `show RUN` · `nodes RUN` · `cancel RUN` · `retry RUN` · `rebuild RUN` | Läufe untersuchen und steuern |
| `trigger process` | Linien-Trigger einmal auswerten |
| `schedule tick` · `dlq ls` · `quarantine BEAN --reason TEXT` · `release BEAN` | Scheduler, Dead Letters, Quarantäne |
| `approval ls [--job J] [--status S]` · `show ID` · `decide ID approve\|deny --by NAME [--note TEXT]` | Freigabepunkte |

## Integration

| Befehl | Beschreibung |
|---|---|
| `image build …` · `image ls` | Maschinen-Images bauen und auflisten (vom Foreman erstellte Dockerfiles) |
| `gateway manifest MACHINE@V` | das Service-Gateway-Manifest für eine Maschine der Form B |
| `egress check STATION@N` | den effektiven Egress-Plan einer Station anzeigen |
| `secret ls` | Namen und Klassen von Secrets (niemals Werte) |
| `backend status` · `migrate …` · `verify` | Speicher-Backend: lokal ↔ S3 + Postgres |
| `backup create` · `verify` · `restore` | Backups von Zustand und Blobs |

## Autonomie

| Befehl | Beschreibung |
|---|---|
| `serve [--once]` | der Daemon: Trigger, Läufe, Jobs, Governance, Audit, Canaries, Weck-Tickets |
| `foreman next` · `done WAKE` · `get KEY` · `list KIND` · `diff BASE COMMIT` · `bean …` · `job …` · `dlq` · `drift` · `invariant` · `metrics` · `approval` · `changeset` · `review` · `conversion` · `supervise` · `sessions` · `wakes` · `reopen` | die Arbeitsoberfläche des Foreman (Vorarbeiter) |
| `mcp --role foreman\|reviewer` | dieselbe Oberfläche als MCP-Server über stdio |
| `maintain bootstrap` | prüfen, ob die Wartungslinien und `registry-sink` existieren; andernfalls Exit-Code 1 und Liste der fehlenden Teile |

## Beobachtbarkeit

| Befehl | Beschreibung |
|---|---|
| `status` | ein JSON-Schnappschuss der gesamten Fabrik |
| `metrics` | Prometheus-Textformat |
| `events [--since SEQ] [--kind K] [--limit N]` | das Ereignisprotokoll |
| `console [--port N]` | lokale, schreibgeschützte Webkonsole (Standard `127.0.0.1:8090`) |

## `mrl`

| Befehl | Beschreibung |
|---|---|
| `mrl eval EXPR` · `mrl template TEXT` | einen Ausdruck auswerten / eine Vorlage rendern |
| `mrl check MODULE` · `mrl run MODULE` | ein Regelmodul statisch prüfen / auswerten |
| `mrl registry …` | Prüfungen und Abfragen der Registry (`mrl registry check --strict`) |
| `mrl resolve …` | Konvertierungen für eine hypothetische Kante auflösen |
| `mrl explain CODE` | einen Diagnosecode wie `E-NO-CONVERTER` erklären |
| `mrl conformance` · `mrl coverage` | die Konformitätssuite ausführen / die Abdeckung der Fabrikfälle anzeigen |
