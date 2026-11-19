# Entwicklung

Code, `spec/` und Abnahmetests dürfen in der Reihenfolge geändert werden, die die Verifikation erfordert. Eine Spezifikation muss nicht vor ihrer Implementierung committed werden.

## Verifikation vor dem Einreichen

Vor einem Commit oder Push ausführen:

```bash
uv run python tools/harness.py verify
```

`verify` prüft MRL-Fehlercodes und Standardbibliotheksfunktionen, das Factory-Profil, die Kapitelstruktur von `spec/system` sowie die gegenseitige Zuordnung von Kapitel 7 und `tests/acceptance/`. Danach laufen die zugeordneten Abnahmetests, MRL-Conformance, MRL-Coverage und die Code-Policy-Tests. Die zugeordneten Abnahmetests müssen ohne Fehler, Exceptions oder Überspringen durchlaufen.

## Arbeitsablauf

Wenn Code, Spezifikation und Tests nicht übereinstimmen, wird die Abweichung in derselben Änderung behoben und `verify` erneut ausgeführt. Das Repository verlangt keine Aufgabenübernahme, keinen Aufgabenstart, keine Stage-Gates, keinen Aufgabenstatus, keine Schutzdateisperren, keine Review-Artefakte sowie keine PreToolUse- oder Git-Hooks mehr. Historische Dateien unter `plan/tasks/`, `plan/journal/` und die Meilensteindateien bleiben als Kontext erhalten, sind aber kein aktueller Einstiegspunkt. `verify` liest sie nicht.

## Technische Regeln

Keine neuen Abhängigkeiten hinzufügen, keine LLM-API aus der Control Plane aufrufen, kein `shell=True` verwenden und Tests nicht abschwächen. Zugangsdaten gehören nicht in Logs, Commits oder Antworten. Unabhängige Änderungen bleiben erhalten. Commit und Push nur auf ausdrückliche Aufforderung.
