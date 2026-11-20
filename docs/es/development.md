> **Migration notice.** The active control plane uses Bun + TypeScript with Podman or headless Kiri. This translated document retains historical Python milestone examples. Use the active English or Chinese guides for current commands.

# Desarrollo

El código, `spec/` y las pruebas de aceptación pueden modificarse en el orden que exija la verificación. La especificación no tiene que confirmarse antes de implementar.

## Verificar antes de entregar

Ejecuta este comando antes de hacer commit o push:

```bash
uv run python tools/harness.py verify
```

`verify` comprueba los códigos de error y las funciones de biblioteca estándar de MRL, el perfil de factory, la estructura de capítulos de `spec/system` y la correspondencia bidireccional entre el capítulo 7 y `tests/acceptance/`. Después ejecuta las pruebas de aceptación relacionadas, la conformidad MRL, la cobertura MRL y las pruebas de política del código. Las pruebas de aceptación relacionadas deben tener cero fallos, errores y omisiones.

## Flujo de edición

Cuando el código, la especificación y las pruebas no coincidan, corrige la diferencia en el mismo cambio y vuelve a ejecutar `verify`. El repositorio ya no exige reclamar tareas, iniciar tareas, pasar stage gates, mantener estados de tareas, usar bloqueos de archivos protegidos, crear artefactos de revisión ni instalar hooks PreToolUse o Git. Los materiales históricos de `plan/tasks/`, `plan/journal/` y los hitos se conservan como contexto, pero no son una entrada de implementación actual y `verify` no los lee.

## Reglas técnicas

No añadas dependencias, no llames a una API de LLM desde el control plane, no uses `shell=True` ni debilites las pruebas. Mantén las credenciales fuera de registros, commits y respuestas. Conserva los cambios no relacionados. Haz commit y push solo cuando se solicite explícitamente.
