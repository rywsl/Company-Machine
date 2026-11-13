[English](../en/concepts.md) | [日本語](../ja/concepts.md) | [한국어](../ko/concepts.md) | [Français](../fr/concepts.md) | **Deutsch** | [Español](../es/concepts.md) | [Português (BR)](../pt-BR/concepts.md) | [Русский](../ru/concepts.md) | [简体中文](../zh-CN/concepts.md)

# Konzepte

Machine Agent Org hat ein kleines Metamodell. Alles, was Sie bauen können (eine Videopipeline, eine Code-Review-Linie, einen nächtlichen Bericht), wird mit den sieben Grundbausteinen unten ausgedrückt, geschrieben als YAML-**Specs** in einer Git-Registry. Der Strukturcompiler prüft diese Specs so, wie ein Typprüfer Code prüft.

> Begründung des Entwurfs: [`docs/architecture.md`](../architecture.md), Abschnitte 3–10 (Chinesisch). Formale Regeln: [`spec/mrl/factory-profile.md`](../../spec/mrl/factory-profile.md).

## 1. Die Grundbausteine

| Grundbaustein | Bedeutung | Kernpunkte |
|---|---|---|
| **Type** (Typ) | die Spezifikation eines Materials | `name@major.minor`. Arten: atomar, `bundle{…}`, `list<T>`, `ref<T>`, `any`. Trägt ein Nutzdaten-Layout, ein Metadatenschema, Validatoren (mit Accept-/Reject-Fixtures), gemessene *Fakten* und benannte *Eigenschaften* |
| **Bean** (Material) | ein Stück Material | inhaltsadressiert (`sha256:…`), unveränderlich, mit Lineage (Herkunftskette) und Provenienz versiegelt |
| **Machine** (Maschine) | ein vorhandenes Produkt | beschrieben durch ein Machine Package: per Digest fixiertes Image, Rezepte, Ein- und Ausgaben, Probes, Konformitäts-Goldens |
| **Operator** | wer die Maschine bedient | eine Agenten-CLI (Claude Code headless, Codex), ein Skript oder ein Mensch über einen Posteingang |
| **Station** | Recipe × Operator × Policy | typisierte Ports. Rollen: `source`, `transform`, `sink`; `converter`, `judge` und `planner` sind spezialisierte Transformationen |
| **Line** (Linie) | ein typisierter DAG aus Stationen | plus begrenzte dynamische Konstrukte. Eine Linie mit Schnittstelle kann als Station verwendet werden (`subline`) |
| **Policy** | Berechtigungen | Egress-Stufe, Secrets, Budget, Freigaben, Veröffentlichung, Aufbewahrung |

Darüber liegen drei Konzepte auf Metaebene. Eine **Spec** ist jede der obigen Definitionen, gespeichert als Bean vom Typ `spec.<kind>@N`. Eine **Invariant** ist eine in MRL geschriebene Regel der Verfassung. Ein **ChangeSet** ist ein Diff von Specs und selbst eine Bean. Weil die Struktur der Fabrik selbst Material ist, können dieselben Prüfungen auch Wartungsarbeiten absichern. So wird der Foreman (Vorarbeiter) in Schach gehalten.

## 2. Typen, Fakten und Eigenschaften

Ein Typ deklariert *Fakten*, die Validatoren messen, und *Eigenschaften*, also benannte MRL-Ausdrücke über diesen Fakten.

```yaml
# spec/examples/registry/types/video.yaml (Auszug)
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

Jeder Validator wird mit Accept- und Reject-Fixtures ausgeliefert. Eine Änderung, durch die eine Reject-Fixture besteht und damit schlechtes Material durchgelassen würde, wird automatisch als hohes Risiko eingestuft.

## 3. Konsumentenverträge: `guarantees` und `requires`

Ports formulieren Verträge über Eigenschaften. Ein Ausgabeport listet auf, was er **garantiert** (`guarantees`), und das Gate (Ausgangsprüfung) setzt es durch. Ein Eingabeport listet auf, was er **verlangt** (`requires`).

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

Zur Kompilierzeit muss jede Kante `requires ⊆ guarantees ∪ type properties` erfüllen. Das ist eine Mengeninklusion: Wenn eine vorgelagerte Station eine Garantie streicht, nennt der Compiler genau die nachgelagerten Ports, die dadurch brechen. Zur Laufzeit wertet die Eingangsprüfung dieselben Eigenschaften an der tatsächlichen Bean aus.

## 4. Konvertierung: Produkte werden automatisch zu Material

Passen Typen oder Eigenschaften einer Kante nicht zusammen, durchsucht der Compiler die Registry nach **Konverter**-Stationen (`converter`) und fügt genau eine Kette ein. Kommen mehrere Ketten infrage, wählt er nach diesen Regeln in dieser Reihenfolge: zuerst verlustfrei, dann die wenigsten Schritte, dann die niedrigsten geschätzten Kosten, dann die kürzeste geschätzte Dauer, dann der Name. Sie haben das in [Erste Schritte](getting-started.md) gesehen: `note.raw@1 → note.text@1` wurde durch `note-to-text@1` überbrückt. Gibt es keine Kette, rät der Compiler nicht. Er meldet `E-NO-CONVERTER`, und eine *Konvertierungsanfrage* weckt den Foreman, der einen neuen Konverter vorschlagen kann.

## 5. Linien

```yaml
# spec/examples/registry/lines/explainer-multilang.yaml (Auszug)
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

Die Regeln:
- Jedes Endprodukt muss von einem Sink (veröffentlichende Station) verbraucht oder in `keep` aufgeführt werden.
- Budgets sind hierarchisch: global → Linie → Lauf → Station → Job.
- Trigger können `bean`-Eingänge, `cron`- oder `every`-Zeitpläne oder manuelle Auslöser sein.

### Begrenzte dynamische Struktur

| Konstrukt | Was es tut | Grenze |
|---|---|---|
| `map` | führt für jedes Element einer `list<T>` einen Teilschritt aus | `max_items` (bei Überlauf ablehnen oder abschneiden), `max_parallel` |
| `switch` | wählt einen Zweig anhand einer MRL-Bedingung | muss vollständig sein, mit `default` |
| `until` | wiederholt einen Teilschritt, bis eine MRL-Bedingung erfüllt ist | `max_iter`; wird es ausgeschöpft, schlägt der Lauf fehl |
| `subline` | führt eine andere Linie innerhalb der Epoch dieses Laufs aus | Tiefenbegrenzung |
| `plan` | eine **Planner**-Station gibt einen `Plan` (einen Teil-DAG) aus | mit V1/V2 kompiliert und gegen `plan_bounds` geprüft, bevor irgendetwas läuft |

## 6. Epochs: ändern, ohne laufende Arbeit zu stören

Jeder Registry-Commit, der die Prüfung besteht, kann zu einer **Epoch** (einem eingefrorenen Strukturstand) werden. Ein Lauf bindet sich beim Start an eine Epoch und behält sie bis zum Ende; Strukturänderungen wirken sich daher nie auf laufende Arbeit aus. Ein Rollback bedeutet, den Zeiger auf die aktuelle Epoch zurückzusetzen. Mehrere Epochs können gleichzeitig in Betrieb sein, zum Beispiel die aktive Epoch neben einem Canary.

## 7. Sechs Prüfschichten

| Schicht | Was geprüft wird | Wann | Bei Fehlschlag |
|---|---|---|---|
| **V1** Spec | Schema, Referenzen, fixierte Versionen, Policy-Lint | bei jedem Vorschlag | Änderung ablehnen |
| **V2** Struktur | Kantentypen und Verträge, Zyklen, Pflichteingaben, verbrauchte Ausgaben, Berechtigungen, Budget, dynamische Grenzen | Vorschlag + vor jedem Lauf | Änderung / Lauf ablehnen |
| **V3** Evolution | Kompatibilität mit der Vorversion, alle Verträge nachgelagerter Konsumenten, Auswirkungsradius | Vorschlag | ablehnen oder einen Migrationskonverter verlangen |
| **V4** Vertragstests | Maschinenkonformität, Golden Briefs, Konverter-Round-Trips, Sink-Probeläufe | Vorschlag + geplante Canaries | ablehnen oder einen Drift-Alarm auslösen |
| **V5** Laufzeit | Eingangsprüfung, Gate (Struktur → Validator → Akzeptanz → Judge), Prüfung der Sink-Quittungen | jeder Job | ablehnen → erneuter Versuch, oder DLQ |
| **V6** Kontinuierlich | Invarianten-Audit, Drift-Überwachung, Integrität von CAS und Lineage, Abgleich | periodisch + Ereignisse | Quarantäne, Circuit-Breaker, Rollback, Foreman wecken |

## 8. Vier Arten von Drift

| Drift | Beispiel | Erkannt durch |
|---|---|---|
| Spec-Drift | jemand bearbeitet einen Typ oder einen Port | V1–V3 und Konsumentenverträge |
| Verhaltensdrift | das Produkt wird aktualisiert, Modell oder Prompt ändern sich | fixierte Versionen, V4-Canaries, Shadow-Läufe |
| Semantische Drift | die Ausgabe besteht das Schema noch, aber die Qualität sinkt | Akzeptanzausdrücke, stichprobenartige Judges, Metrik-Baselines |
| Konfigurationsdrift | Container oder Secret-Bindungen am Prozess vorbei geändert | der Reconciler und das Invarianten-Audit |

## 9. Governance-Stufen

Die Prüfpipeline leitet aus dem Diff jedes ChangeSets deterministisch eine Risikostufe ab. Niemand kann sie senken, auch nicht der Foreman.

| Stufe | Typische Änderung | Anforderungen |
|---|---|---|
| T0 | Wiederholung, Quarantäne | automatisch |
| T1 | Template-Feinschliff, neuer Konverter, kleine Typversion | Prüfung + Shadow-Lauf |
| T2 | Maschine aufnehmen oder aktualisieren, neue Linie, große Typversion | + Canary + Kreuz-Review durch eine andere Laufzeit |
| T3 | neue Source/neuer Sink, Egress- oder Credential-Bindung, Policy-Änderung, Stilllegung | + menschliche Freigabe |
| T4 | die Verfassung | nur Menschen |

Die Verfassung (L0) ist der letzte Anker. Maschinen und Agenten können alles andere umgestalten, aber nur über geprüfte, überprüfbare und umkehrbare ChangeSets.
