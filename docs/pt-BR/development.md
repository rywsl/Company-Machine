> **Migration notice.** The active control plane uses Bun + TypeScript with Podman or headless Kiri. This translated document retains historical Python milestone examples. Use the active English or Chinese guides for current commands.

# Desenvolvimento

Código, `spec/` e testes de aceitação podem ser alterados na ordem exigida pela verificação. A spec não precisa ser commitada antes da implementação.

## Verificação antes do envio

Execute este comando antes de fazer commit ou push:

```bash
bun run verify:bun
```

`verify` verifica os códigos de erro e as funções da biblioteca padrão do MRL, o factory profile, a estrutura de capítulos de `spec/system` e o mapeamento bidirecional entre o capítulo 7 e `tests/acceptance/`. Depois executa os testes de aceitação relacionados, MRL conformance, MRL coverage e os testes de política do código. Os testes de aceitação relacionados devem ter zero falhas, erros e skips.

## Fluxo de edição

Quando código, spec e testes divergirem, corrija a divergência na mesma alteração e execute `verify` novamente. O repositório não exige mais obter ou iniciar tarefas, passar stage gates, manter estado de tarefas, usar locks de arquivos protegidos, criar artefatos de revisão ou instalar hooks PreToolUse ou Git. Os materiais históricos em `plan/tasks/`, `plan/journal/` e os marcos permanecem como contexto, mas não são uma entrada atual de implementação e não são lidos por `verify`.

## Regras técnicas

Não adicione dependências, não chame uma API de LLM no control plane, não use `shell=True` e não enfraqueça os testes. Mantenha credenciais fora de logs, commits e respostas. Preserve alterações não relacionadas. Faça commit e push somente quando solicitado explicitamente.
