"""M1 执行面验收测试的共享夹具（受保护；只能由人修改）。导入本模块不会加载 mao。

- open_factory：FakeClock + CounterIds + 带 local_commands() 的 LocalProvider，返回 (ctx, snapshot, runner)；
- seal_note / seal_task：把文本或 fake agent 脚本收成已封存的输入 Bean（facts 与夹具 validator 的算法一致）；
- read_payload：读出 Bean 某个 payload 文件的正文。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tests.fakes.fakelib import local_commands, render_registry

START = datetime(2026, 11, 10, tzinfo=UTC)
REPORT = "sha256:" + "ab" * 32


def note_facts(text: str) -> dict[str, Any]:
    return {
        "chars": len("".join(text.split())),
        "lines": len(text.splitlines()),
        "has_lower": any(ch.islower() for ch in text),
    }


def task_steps(script: dict[str, Any]) -> int:
    total = len(script.get("default", []))
    for actions in (script.get("attempts") or {}).values():
        total += len(actions)
    return total


def open_factory(tmp: Path, *, provider: Any = None, images: dict[str, str] | None = None):
    from mao.base import CounterIds, FakeClock
    from mao.context import Context
    from mao.runner import Runner
    from mao.sandbox import LocalProvider
    from mao.snapshot import Snapshot

    root = tmp / "factory"
    root.mkdir(parents=True, exist_ok=True)
    clock = FakeClock(START)
    provider = provider or LocalProvider(root, commands=local_commands(), clock=clock)
    ctx = Context.open(root, provider=provider, clock=clock, ids=CounterIds())
    snapshot = Snapshot.load(render_registry(tmp / "registry", images=images))
    return ctx, snapshot, Runner(ctx, snapshot)


def _seal(ctx: Any, type_id: str, name: str, data: bytes, facts: dict[str, Any], tmp: Path) -> str:
    from mao.warehouse import BeanDraft

    src = tmp / "incoming" / hashlib.sha256(data).hexdigest()[:16] / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(data)
    bean = ctx.warehouse.add(BeanDraft(type=type_id, meta={}, files={name: src}))
    ctx.warehouse.seal_many([(bean.id, facts)], job_id=None, report_digest=REPORT)
    return bean.id


def seal_note(ctx: Any, tmp: Path, text: str) -> str:
    return _seal(ctx, "note.text@1.0", "note.txt", text.encode("utf-8"), note_facts(text), tmp)


def seal_task(ctx: Any, tmp: Path, script: dict[str, Any]) -> str:
    data = json.dumps(script, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return _seal(ctx, "agent.task@1.0", "task.json", data, {"steps": task_steps(script)}, tmp)


def read_payload(ctx: Any, bean_id: str, path: str) -> str:
    bean = ctx.warehouse.get(bean_id)
    item = next(p for p in bean.payload if p.path == path)
    return ctx.warehouse.blob_path(item.digest).read_text(encoding="utf-8")
