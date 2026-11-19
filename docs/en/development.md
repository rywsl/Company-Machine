# Development

Code, `spec/` and acceptance tests may be changed in the order required by verification. A specification does not have to be committed before its implementation.

## Verify before submitting

Run this command before committing or pushing:

```bash
uv run python tools/harness.py verify
```

`verify` checks MRL error codes and standard-library functions, the factory profile, `spec/system` chapter structure, and the two-way mapping between chapter 7 and `tests/acceptance/`. It then runs the mapped acceptance tests, MRL conformance, MRL coverage, and the code policy tests. Mapped acceptance tests must have zero failures, errors, and skips.

## Editing workflow

When code, specification, and tests disagree, fix the disagreement in the same change and run `verify` again. The repository no longer requires task claiming, task start, stage gates, task status, protected-file locks, review artifacts, PreToolUse hooks, or Git hooks. Historical material in `plan/tasks/`, `plan/journal/`, and milestone files remains available for context, but it is not a current implementation entry point and `verify` does not read it.

## Engineering rules

Do not add dependencies, call an LLM API from the control plane, use `shell=True`, or weaken tests to make verification pass. Keep credentials out of logs, commits, and replies. Preserve unrelated changes. Commit and push only when explicitly requested.
