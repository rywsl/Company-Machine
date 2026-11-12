"""M9 验收的建库辅助（spec/system/closure.md 第 7 章）。导入本模块不会加载 mao。

在 tests/fixtures/registry-m2 的副本上增加：拆分工位 split@1（输出 list<note.text@1>）、动态产线、
planner 工位与 plan.doc@1 类型、服务型配方 render。拆分工具是测试运行时写出的标准库脚本，
按 gate.md 的列表端口布局写 /factory/out/notes/<下标>/note.txt，经 LocalProvider 的命令映射执行，不进入测试镜像。
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from tests.acceptance.m2.world import FIXTURE

SPLIT_TOOL = """import sys
from pathlib import Path

src, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
rows = [row for row in src.read_text(encoding="utf-8").splitlines() if row.strip()]
for index, row in enumerate(rows):
    item = out / str(index)
    item.mkdir()
    (item / "note.txt").write_text(row + "\\n", encoding="utf-8")
"""

NOTE_IN = "    inputs: {note: {type: note.text@1, requires: [non_empty]}}\n"

SPLIT_RECIPE = """    split:
      inputs:  {note: {type: note.text@1}}
      outputs: {notes: {type: "list<note.text@1>"}}
      run: [split-lines, /factory/in/note/note.txt, /factory/out/notes]
"""

PLAN_RECIPE = """    plan:
      inputs:
        task: {type: agent.task@1}
      outputs:
        plan: {type: plan.doc@1, harvest: "plans/plan.json"}
      task_template: |
        按照 /factory/in/task/task.json 写出 plans/plan.json。
"""

SERVICEBOX = """apiVersion: mao/v1
kind: Machine
metadata: {name: servicebox, version: 1}
spec:
  form: service
  runtime:
    image: mao-test/tool@sha256:09b42648636a8405354592f2ae8985670dfda5b61377ac6c75e3cebbd22e0500
    instance: per_job
    resources: {cpus: 1, memory: 256Mi}
    service: {name: api, port: 8080}
  operate:
    long_task:
      submit: "PUT /v2/renders"
      poll: "GET /v2/renders/{task_id}/status"
      task_id: "/data/ref"
      done: 'response.state == "finished"'
      fetch: response.outputs
      poll_every: "2s"
  needs: {egress: none}
  recipes:
    pull:
      outputs: {note: {type: note.text@1, harvest: /factory/out/note/note.txt}}
      run: [upper-text, /factory/in/note/note.txt, /factory/out/note/note.txt]
    render:
      inputs:  {note: {type: note.text@1}}
      outputs: {clip: {type: note.text@1}}
      call:
        chars: "{{ inputs.note.facts.chars }}"
        lang: en
"""

STATIONS = {
    "split": """apiVersion: mao/v1
kind: Station
metadata: {name: split, version: 1}
spec:
  role: transform
  machine: toolbox@1
  recipe: split
  operator: {runtime: script@1}
  inputs:  {note: {type: note.text@1, requires: [non_empty]}}
  outputs: {notes: {type: "list<note.text@1>", guarantees: [non_empty]}}
  limits:  {timeout: 60s, budget_usd: 0, retries: {rejected: 0, failed: 0, mode: fresh}}
  policy: tool-default@1
""",
    "planner": """apiVersion: mao/v1
kind: Station
metadata: {name: planner, version: 1}
spec:
  role: planner
  machine: studio@1
  recipe: plan
  operator: {runtime: fake-agent@1, model: fake-model-1, max_turns: 5}
  inputs:  {task: {type: agent.task@1, requires: [has_steps]}}
  outputs: {plan: {type: plan.doc@1}}
  limits:  {timeout: 120s, budget_usd: 0.5, retries: {rejected: 1, failed: 0, mode: fresh}}
  policy: agent-default@1
  plan_bounds:
    allowed_stations: [upper@1, write@1, publish-local@1, planner@1]
    allowed_sinks: [publish-local@1]
    max_steps: 4
    max_fanout: 3
    max_depth: 1
    budget_usd: 2.0
""",
    "render": """apiVersion: mao/v1
kind: Station
metadata: {name: render, version: 1}
spec:
  role: transform
  machine: servicebox@1
  recipe: render
  operator: {runtime: script@1}
  inputs:  {note: {type: note.text@1, requires: [non_empty]}}
  outputs: {clip: {type: note.text@1, guarantees: [non_empty]}}
  limits:  {timeout: 60s, budget_usd: 0, retries: {rejected: 0, failed: 0, mode: fresh}}
  policy: tool-default@1
""",
}

PLAN_TYPE = """apiVersion: mao/v1
kind: Type
metadata: {name: plan.doc, version: "1.0"}
spec:
  payload:
    - {path: plan.json, media_type: application/json, required: true}
"""


def _line(name: str, body: str) -> str:
    return f"apiVersion: mao/v1\nkind: Line\nmetadata: {{name: {name}, version: 1}}\nspec:\n{body}"


def _map_line(name: str, map_body: str) -> str:
    return _line(
        name,
        "  interface:\n" + NOTE_IN + '    outputs: {notes: {from: shout.note, type: "list<note.text@1>"}}\n'
        "  budget: {cap_usd: 1.0}\n"
        "  steps:\n"
        "    split: {station: split@1, in: {note: $inputs.note}}\n"
        "    shout:\n"
        "      station: upper@1\n"
        "      in: {note: $item}\n"
        f"      map: {map_body}\n",
    )


def _until_line(name: str, cond: str, max_iter: int) -> str:
    return _line(
        name,
        "  interface:\n" + NOTE_IN + "    outputs: {note: {from: shout.note, type: note.text@1}}\n"
        "  budget: {cap_usd: 1.0}\n"
        "  steps:\n"
        "    shout:\n"
        "      station: upper@1\n"
        "      in: {note: $inputs.note}\n"
        f"      until: {{cond: '{cond}', max_iter: {max_iter}}}\n",
    )


LINES = {
    "note-split": _line(
        "note-split",
        "  interface:\n" + NOTE_IN + '    outputs: {notes: {from: split.notes, type: "list<note.text@1>"}}\n'
        "  budget: {cap_usd: 1.0}\n"
        "  steps:\n"
        "    split: {station: split@1, in: {note: $inputs.note}}\n",
    ),
    "note-map": _map_line("note-map", "{over: split.notes, max_items: 3}"),
    "note-map-reject": _map_line("note-map-reject", "{over: split.notes, max_items: 2}"),
    "note-map-trunc": _map_line("note-map-trunc", "{over: split.notes, max_items: 2, overflow: truncate}"),
    "note-map-serial": _map_line("note-map-serial", "{over: split.notes, max_items: 3, max_parallel: 1}"),
    "note-switch": _line(
        "note-switch",
        "  interface:\n" + NOTE_IN + "  budget: {cap_usd: 1.0}\n"
        "  keep: [loud.note, calm.note]\n"
        "  steps:\n"
        "    choose:\n"
        "      switch:\n"
        "        on: $inputs.note\n"
        "        cases:\n"
        "          - {when: 'has(bean.meta.title) && bean.meta.title == \"loud\"', goto: loud}\n"
        "        default: calm\n"
        "    loud: {station: upper@1, in: {note: $inputs.note}}\n"
        "    calm: {station: upper@1, in: {note: $inputs.note}}\n",
    ),
    "note-until": _until_line("note-until", "gate.attempt >= 2", 3),
    "note-until-never": _until_line("note-until-never", "gate.attempt > 5", 2),
    "note-child": _line(
        "note-child",
        "  interface:\n" + NOTE_IN + "    outputs: {note: {from: shout.note, type: note.text@1}}\n"
        "  budget: {cap_usd: 0.5}\n"
        "  steps:\n"
        "    shout: {station: upper@1, in: {note: $inputs.note}}\n",
    ),
    "note-parent": _line(
        "note-parent",
        "  interface:\n" + NOTE_IN + "    outputs: {note: {from: child.note, type: note.text@1}}\n"
        "  budget: {cap_usd: 1.0}\n"
        "  steps:\n"
        "    child: {subline: note-child@1, in: {note: $inputs.note}}\n",
    ),
    "note-plan": _line(
        "note-plan",
        "  interface:\n"
        "    inputs:\n"
        "      task: {type: agent.task@1, requires: [has_steps]}\n"
        "      note: {type: note.text@1, requires: [non_empty]}\n"
        "  budget: {cap_usd: 2.5}\n"
        "  keep: [plan.plan]\n"
        "  steps:\n"
        "    plan: {station: planner@1, in: {task: $inputs.task}}\n",
    ),
    "note-render": _line(
        "note-render",
        "  interface:\n" + NOTE_IN + "    outputs: {clip: {from: render.clip, type: note.text@1}}\n"
        "  budget: {cap_usd: 1.0}\n"
        "  steps:\n"
        "    render: {station: render@1, in: {note: $inputs.note}}\n",
    ),
}


def _append(path: Path, text: str) -> None:
    body = path.read_text(encoding="utf-8").rstrip("\n") + "\n"
    path.write_text(body + text, encoding="utf-8", newline="\n")


def build_registry(dest: Path) -> Path:
    """registry-m2 的副本加上本文件的工位、产线与类型。调用方应先确认 `mrl registry check` 零 error。"""
    registry = dest / "registry"
    if registry.exists():
        shutil.rmtree(registry)
    shutil.copytree(FIXTURE, registry)
    _append(registry / "machines" / "toolbox" / "machine.yaml", SPLIT_RECIPE)
    _append(registry / "machines" / "studio" / "machine.yaml", PLAN_RECIPE)
    (registry / "machines" / "servicebox" / "machine.yaml").write_text(
        SERVICEBOX, encoding="utf-8", newline="\n"
    )
    (registry / "types" / "plan.yaml").write_text(PLAN_TYPE, encoding="utf-8", newline="\n")
    for name, text in STATIONS.items():
        (registry / "stations" / f"{name}.yaml").write_text(text, encoding="utf-8", newline="\n")
    for name, text in LINES.items():
        (registry / "lines" / f"{name}.yaml").write_text(text, encoding="utf-8", newline="\n")
    return registry


def open_world(
    tmp: Path, registry: Path, *, commands: dict[str, list[str]] | None = None, gateway: bool = False
):
    """返回 (ctx, snapshot, runner, clock)。gateway 为真时 Runner 装上 ServiceGateway（closure.md 5.3）。"""
    from mao.base import CounterIds, FakeClock
    from mao.context import Context
    from mao.runner import Runner
    from mao.sandbox import LocalProvider
    from mao.snapshot import Snapshot
    from tests.fakes.fakelib import local_commands

    tools = tmp / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    split = tools / "split_lines.py"
    split.write_text(SPLIT_TOOL, encoding="utf-8")
    mapping = local_commands()
    mapping["split-lines"] = [sys.executable, str(split)]
    mapping.update(commands or {})
    root = tmp / "factory"
    root.mkdir(parents=True)
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    provider = LocalProvider(root, commands=mapping, clock=clock)
    ctx = Context.open(root, provider=provider, clock=clock, ids=CounterIds())
    snapshot = Snapshot.load(registry)
    if gateway:
        from mao.gateway import ServiceGateway

        runner = Runner(ctx, snapshot, gateway=ServiceGateway(ctx, snapshot))
    else:
        runner = Runner(ctx, snapshot)
    return ctx, snapshot, runner, clock


def _seal(ctx, snapshot, type_ref: str, files: dict[str, bytes], meta: dict, facts: dict) -> str:
    from mao.warehouse import BeanDraft

    type_id = snapshot.type_key(type_ref).split(":", 1)[1]
    incoming = ctx.root / "incoming" / ctx.ids.new("in")
    paths = {}
    for rel, data in files.items():
        path = incoming / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        paths[rel] = path
    bean = ctx.warehouse.add(BeanDraft(type=type_id, meta=meta, files=paths))
    ctx.warehouse.seal_many([(bean.id, facts)], job_id=None, report_digest="sha256:" + "0" * 64)
    return bean.id


def seal_note(ctx, snapshot, text: str, *, title: str | None = None) -> str:
    facts = {"chars": len(text), "lines": len(text.splitlines()), "has_lower": any(c.islower() for c in text)}
    meta = {"title": title} if title is not None else {}
    return _seal(ctx, snapshot, "note.text@1", {"note.txt": text.encode("utf-8")}, meta, facts)


def seal_task(ctx, snapshot, script: dict) -> str:
    data = json.dumps(script, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return _seal(ctx, snapshot, "agent.task@1", {"task.json": data}, {}, {"steps": 1})


def plan_doc(steps: dict, *, keep: list[str] | None = None) -> dict:
    spec: dict = {"steps": steps}
    if keep is not None:
        spec["keep"] = keep
    return {"apiVersion": "mao/v1", "kind": "Plan", "metadata": {"name": "acceptance-plan"}, "spec": spec}


def planner_script(*docs: dict) -> dict:
    """第 n 次 attempt 写出 docs[n-1]；次数超过 docs 时重复最后一份。"""

    def write(doc: dict) -> list[dict]:
        return [{"do": "write", "path": "plans/plan.json", "text": json.dumps(doc, ensure_ascii=False)}]

    return {
        "attempts": {str(i): write(doc) for i, doc in enumerate(docs, 1)},
        "default": write(docs[-1]),
    }


SHOUT = {"shout": {"station": "upper@1", "in": {"note": "$inputs.note"}}}


def node_map(lines, run_id: str) -> dict:
    return {node.node_id: node for node in lines.nodes(run_id)}


def bean_text(ctx, bean_id: str, rel: str = "note.txt") -> str:
    ref = next(item for item in ctx.warehouse.get(bean_id).payload if item.path == rel)
    return ctx.warehouse.blob_path(ref.digest).read_text(encoding="utf-8")
