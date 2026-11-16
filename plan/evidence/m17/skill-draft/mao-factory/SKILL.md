---
name: mao-factory
description: Operate an existing Machine Agent Org factory through its public CLI, hand authorized goals to factory agents, and inspect progress, blockers, recovery, and results. Use for factory operations; repository implementation follows the project development workflow.
---

# Operate a MAO factory

This is an M17 review draft, written against the c538768 command surface. It has not passed independent user testing or been installed as a published skill. M17 continuous-goal commands are not available in that baseline; do not invent them or claim M15 talk grants lasting execution authority.

## Discover the installed interface

Identify the executable, version, factory root, Registry, provider and actor authorized by the user. Preserve an existing installation. Use `factory --version`, `factory --help` and the relevant subgroup's `--help` before constructing commands. For a source checkout, `uv run factory` is the equivalent entrypoint.

`factory` is the control plane. `factoryctl` is the restricted tool inside a station sandbox. `mrl` checks expressions and Registry contracts. `tools/harness.py` is the repository development/acceptance tool; its doctor/status do not establish that a deployed factory is ready or autonomous.

Read the installed version's getting-started and runtime documentation for setup requirements. The repository's local fake-agent tutorial is a protocol demonstration. Do not copy it over an existing Registry or present it as real model integration. If the installed interface cannot configure or diagnose a required capability, name that gap and retain the evidence; ordinary factory operations do not authorize patching the platform source.

## Hand work to the factory

Use the caller's established root/provider flags consistently. Check `factory status`, `factory agent list` and relevant subgroup help to determine readiness. Starting `factory serve` changes runtime state: do it when operating this factory is authorized and its configuration is ready. Readiness requires the configured agents and their actual CLI/runtime, model access, permissions and governance, not only a live serve process.

For the current talk interface, discover `factory talk send --help`, `factory talk show --help` and `factory talk ls --help`. Relay the user's goal using the authorized actor; record the returned message ID and observe it using show/ls. A `--by` value is a label, not proof of identity. Do not invent a human identity or confirm a proposal on the user's behalf without authorization.

M15 may propose a run card that needs an actual human confirmation. M17 is intended to add bounded long-term goal authorization; use only the installed, documented capability after its permission and expiry contract is implemented. A text message alone is not a grant to execute every derived action.

The factory agents should discover concrete work, plan it, propose governed changes, assign jobs, execute tools and evaluate results. Do not count a run that the external operator planned, manually advanced or produced itself as factory autonomy. Separately authorized product development is a different task.

## Observe and recover

Use IDs returned by the system. Discover and use `factory agent show`, `factory agent turns`, `factory line show`, `factory line nodes`, `factory job show`, `factory events`, `factory inbox ls` and `factory changeset show` as relevant. Keep the chain from goal/message through agent turns, run/job, outputs, receipts and cost.

Distinguish waiting for an agent, waiting for human permission or a secret, missing runtime/dependencies, running work, terminal failure and an unknown external outcome. For a resource or permission only the user can supply, report the exact blocker and the documented next step. Do not disable audit/canary, lower contracts, overwrite state, directly alter active pointers to bypass governance, or impersonate approval to clear it. Authorized rollback and recovery through the documented governance interface remain valid operations.

A known external task ID should be reconciled and resumed through the supported recovery interface. If submission may have succeeded but its response was lost, preserve the unknown outcome and investigate provider idempotency/status; do not repeat a potentially paid operation blindly. Follow the installed command's actual semantics rather than assuming a generic retry is safe.

## Verify the outcome

Check domain outputs and required quality/receipt evidence, terminal state and costs. A successful process exit, fabricated session ID, one SEALED job or a named-but-unused product is insufficient evidence of sustained autonomous operation. Report what the factory actually did, any external intervention, and remaining blockers. For ordinary continuous operation, report the cycles and limitations actually observed; do not manufacture failures or wait for a full acceptance window. When asked to validate sustained autonomy or run M17 acceptance, use the authorized multi-cycle scenarios, including idle time, a new opportunity and controlled failure recovery.

Repository audit context and pending acceptance requirements are in [the external-agent usage audit](../../external-agent-usage.md) and [the historical coverage matrix](../../historical-coverage.md). These audit references are for this review draft; the released skill must link maintained installed documentation and match the shipped interface.
