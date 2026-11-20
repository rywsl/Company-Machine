> **Migration notice.** The active control plane uses Bun + TypeScript with Podman or headless Kiri. This translated document retains historical Python milestone examples. Use the active English or Chinese guides for current commands.

# Développement

Le code, `spec/` et les tests d’acceptation peuvent être modifiés dans l’ordre imposé par la vérification. La spécification n’a pas besoin d’être commitée avant l’implémentation.

## Vérification avant soumission

Exécuter cette commande avant un commit ou un push :

```bash
bun run verify:bun
```

`verify` contrôle les codes d’erreur et les fonctions de bibliothèque standard de MRL, le profil factory, la structure des chapitres de `spec/system` et la correspondance bidirectionnelle entre le chapitre 7 et `tests/acceptance/`. Il exécute ensuite les tests d’acceptation associés, la conformité MRL, la couverture MRL et les tests de politique du code. Les tests d’acceptation associés doivent avoir zéro échec, erreur ou test ignoré.

## Flux de modification

Quand le code, la spécification et les tests divergent, corriger l’écart dans la même modification puis relancer `verify`. Le dépôt n’exige plus la prise en charge d’une tâche, le démarrage d’une tâche, des stage gates, un état de tâche, des verrous de fichiers protégés, des artefacts de revue ou des hooks PreToolUse ou Git. Les documents historiques de `plan/tasks/`, `plan/journal/` et des jalons restent disponibles pour le contexte, mais ne sont plus une entrée d’implémentation et `verify` ne les lit pas.

## Règles techniques

Ne pas ajouter de dépendance, appeler une API LLM depuis le control plane, utiliser `shell=True` ou affaiblir les tests. Ne pas mettre de secrets dans les journaux, commits ou réponses. Conserver les modifications indépendantes. Commit et push uniquement sur demande explicite.
