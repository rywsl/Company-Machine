"""M10 验收的建库辅助（spec/system/wiring.md 第 7 章）。导入本模块不会加载 mao。

在 tests/fixtures/registry-m2 的副本上增加：几个 Sink 工位（各自用一个测试运行时写出的标准库脚本当操作工，
分别演示模板回执、缺少回执、回执格式错误、回执指向不存在的文件、经 factoryctl receipt 留回执、超时、
factoryctl fail、核验配方崩溃），以及两条以本地目录为发布目标的产线。脚本经 LocalProvider 的命令映射执行，
不进入测试镜像。
"""

from __future__ import annotations

import io
import json
import shutil
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

from tests.acceptance.m2.world import FIXTURE

_COMMON = """import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

src, dest = Path(sys.argv[1]), Path(sys.argv[2])
ctl = Path(os.environ["FACTORY_CTL"])


def copy():
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / src.name
    shutil.copyfile(src, target)
    return target


def digest(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def receipt(body):
    part = ctl / "receipt.json.part"
    part.write_text(json.dumps(body), encoding="utf-8")
    part.replace(ctl / "receipt.json")


def factoryctl(*args):
    return subprocess.run([sys.executable, os.environ["FACTORYCTL"], *args]).returncode
"""

TOOLS = {
    "copy-only": _COMMON + "copy()\n",
    "bad-receipt": _COMMON
    + "copy()\nreceipt({'target': str(dest), 'locator': {'path': str(dest / 'missing.txt')}, 'items': []})\n",
    "broken-receipt": _COMMON + "copy()\nreceipt({'locator': {}})\n",
    "ctl-publish": _COMMON
    + "target = copy()\n"
    + "bean = json.loads((ctl / 'job.json').read_text(encoding='utf-8'))['inputs']['note']['id']\n"
    + "sys.exit(factoryctl('receipt', '--target', str(dest), '--locator', 'path=' + str(target),\n"
    + "                    '--item', bean + '=' + str(target) + '@' + digest(target), '--note', 'by ctl'))\n",
    "slow-publish": "import time\ntime.sleep(30)\n",
    "refuse-publish": _COMMON + "copy()\nsys.exit(factoryctl('fail', 'remote refused'))\n",
    "crash-verify": "import sys\nsys.exit(3)\n",
}

_NOTE = "      inputs:  {note: {type: note.text@1}}\n"
_DIR = "      params:  {dir: {type: string}}\n"


def _recipe(name: str, tool: str, extra: str = "") -> str:
    return (
        f"    {name}:\n{_NOTE}{_DIR}      outputs: {{}}\n"
        f'      run: [{tool}, /factory/in/note/note.txt, "{{{{ params.dir }}}}"]\n{extra}'
    )


RECIPES = (
    _recipe("publish-plain", "copy-only")
    + _recipe(
        "publish-template",
        "copy-only",
        '      receipt:\n        target: "{{ params.dir }}"\n'
        '        locator: {path: "{{ params.dir }}/note.txt", kind: local}\n',
    )
    + _recipe("publish-bad", "bad-receipt")
    + _recipe("publish-broken", "broken-receipt")
    + _recipe("publish-ctl", "ctl-publish")
    + _recipe("publish-slow", "slow-publish")
    + _recipe("publish-refused", "refuse-publish")
    + f"    verify-crash:\n{_NOTE}      outputs: {{}}\n      run: [crash-verify]\n"
)


def _sink(
    name: str, recipe: str, *, verify: str = "local-file-check", timeout: str = "60s", failed: int = 0
) -> str:
    return f"""apiVersion: mao/v1
kind: Station
metadata: {{name: {name}, version: 1}}
spec:
  role: sink
  machine: toolbox@1
  recipe: {recipe}
  operator: {{runtime: script@1}}
  params: {{dir: {{type: string}}}}
  inputs: {{note: {{type: note.text@1}}}}
  outputs: {{}}
  idempotency_key: "{{{{ inputs.note.id }}}}:{{{{ params.dir }}}}"
  verify_receipt: {verify}
  limits: {{timeout: {timeout}, budget_usd: 0, retries: {{rejected: 0, failed: {failed}, mode: fresh}}}}
  policy: sink-local@1
"""


STATIONS = {
    "sink-plain": _sink("sink-plain", "publish-plain"),
    "sink-template": _sink("sink-template", "publish-template"),
    "sink-bad": _sink("sink-bad", "publish-bad"),
    "sink-broken": _sink("sink-broken", "publish-broken"),
    "sink-ctl": _sink("sink-ctl", "publish-ctl"),
    "sink-slow": _sink("sink-slow", "publish-slow", timeout="2s", failed=2),
    "sink-refused": _sink("sink-refused", "publish-refused", failed=2),
    "sink-crashverify": _sink("sink-crashverify", "publish-local", verify="verify-crash"),
}


def _lines(publish_dir: Path) -> dict[str, str]:
    target = json.dumps(str(publish_dir))
    return {
        "note-publish": f"""apiVersion: mao/v1
kind: Line
metadata: {{name: note-publish, version: 1}}
spec:
  interface:
    inputs: {{note: {{type: note.text@1, requires: [non_empty]}}}}
  trigger:
    - {{on: bean, type: note.text@1, label: "line=note-publish"}}
  budget: {{cap_usd: 1.0}}
  steps:
    upper:
      station: upper@1
      in: {{note: $inputs.note}}
    publish:
      station: publish-local@1
      params: {{dir: {target}}}
      in: {{note: upper.note}}
""",
        "task-publish": f"""apiVersion: mao/v1
kind: Line
metadata: {{name: task-publish, version: 1}}
spec:
  interface:
    inputs: {{task: {{type: agent.task@1, requires: [has_steps]}}}}
  budget: {{cap_usd: 5.0}}
  steps:
    write:
      station: write@1
      in: {{task: $inputs.task}}
    upper:
      station: upper@1
      in: {{note: write.note}}
    publish:
      station: publish-local@1
      params: {{dir: {target}}}
      in: {{note: upper.note}}
""",
    }


SCHEDULER = {
    "pools": {"cpu": None, "memory_mb": None, "gpu": None},
    "compartments": [],
    "breakers": [],
    "wip": [],
    "rates": [],
    "run_cap": None,
    "toxic_failures": None,
    "tick_s": None,
}

WRITE_NOTE = {
    "cost_usd": 0.02,
    "default": [{"do": "write", "path": "drafts/note.txt", "text": "hello factory\n"}],
}


def _append(path: Path, text: str) -> None:
    body = path.read_text(encoding="utf-8").rstrip("\n") + "\n"
    path.write_text(body + text, encoding="utf-8", newline="\n")


def build_registry(dest: Path, publish_dir: Path) -> Path:
    """registry-m2 的副本加上本文件的配方、工位与产线。产线的发布目录是 publish_dir。"""
    registry = dest / "registry"
    if registry.exists():
        shutil.rmtree(registry)
    shutil.copytree(FIXTURE, registry)
    _append(registry / "machines" / "toolbox" / "machine.yaml", RECIPES)
    for name, text in STATIONS.items():
        (registry / "stations" / f"{name}.yaml").write_text(text, encoding="utf-8", newline="\n")
    for name, text in _lines(publish_dir).items():
        (registry / "lines" / f"{name}.yaml").write_text(text, encoding="utf-8", newline="\n")
    return registry


def commands(tmp: Path) -> dict[str, list[str]]:
    """LocalProvider 的命令映射：夹具工具、fake agent，加上本文件的脚本。"""
    from tests.fakes.fakelib import local_commands

    tools = tmp / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    mapping = local_commands()
    for name, text in TOOLS.items():
        script = tools / (name.replace("-", "_") + ".py")
        script.write_text(text, encoding="utf-8")
        mapping[name] = [sys.executable, str(script)]
    return mapping


class World:
    """进程内的工厂：LocalProvider + FakeClock + CounterIds，Runner 由 open_runner 构造。"""

    def __init__(self, tmp: Path) -> None:
        from mao.base import CounterIds, FakeClock
        from mao.context import Context
        from mao.runner import open_runner
        from mao.sandbox import LocalProvider
        from mao.snapshot import Snapshot

        self.tmp = tmp
        self.pub = tmp / "published"
        self.registry = build_registry(tmp, self.pub)
        root = tmp / "factory"
        root.mkdir(parents=True)
        self.clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        provider = LocalProvider(root, commands=commands(tmp), clock=self.clock)
        self.ctx = Context.open(root, provider=provider, clock=self.clock, ids=CounterIds())
        self.snapshot = Snapshot.load(self.registry)
        self.runner = open_runner(self.ctx, self.snapshot)

    def key(self, ref: str) -> str:
        return self.snapshot.key("Station", ref)

    def note(self, text: str = "hello factory\n") -> str:
        from mao.warehouse import BeanDraft

        type_id = self.snapshot.type_key("note.text@1").split(":", 1)[1]
        incoming = self.ctx.root / "incoming" / self.ctx.ids.new("in")
        incoming.mkdir(parents=True)
        (incoming / "note.txt").write_text(text, encoding="utf-8")
        bean = self.ctx.warehouse.add(
            BeanDraft(type=type_id, meta={}, files={"note.txt": incoming / "note.txt"})
        )
        facts = {
            "chars": len(text),
            "lines": len(text.splitlines()),
            "has_lower": any(c.islower() for c in text),
        }
        self.ctx.warehouse.seal_many([(bean.id, facts)], job_id=None, report_digest="sha256:" + "0" * 64)
        return bean.id

    def run(self, station: str, note: str, directory: Path | None = None):
        params = {"dir": str(directory or self.pub)}
        return self.runner.run(self.runner.submit(self.key(station), {"note": note}, params).id)

    def receipts(self, job_id: str | None = None) -> list:
        from mao.connectors import ReceiptStore

        found = ReceiptStore(self.ctx).list()
        return [item for item in found if job_id is None or item.job_id == job_id]

    def events(self, job_id: str, prefix: str = "") -> list[tuple[str, dict]]:
        rows = self.ctx.db.query("SELECT kind, data FROM events WHERE job_id = ? ORDER BY seq", (job_id,))
        return [(row["kind"], json.loads(row["data"])) for row in rows if row["kind"].startswith(prefix)]


class Cli:
    """经 `factory` 命令行（mao.cli.main）操作工厂目录；LocalProvider，命令映射写成文件。

    给出 world 时操作该 World 的 Registry 与工厂目录。
    """

    def __init__(self, tmp: Path, *, world: World | None = None) -> None:
        self.tmp = tmp
        self.pub = tmp / "published"
        if world is None:
            self.registry, self.root = build_registry(tmp, self.pub), tmp / "factory"
        else:
            self.registry, self.root = world.registry, world.ctx.root
        mapping = tmp / "commands.json"
        mapping.write_text(json.dumps(commands(tmp)), encoding="utf-8")
        self.base = [
            "--root", str(self.root),
            "--registry", str(self.registry),
            "--provider", "local",
            "--local-commands", str(mapping),
            "--json",
        ]  # fmt: skip

    def __call__(self, *args: str) -> tuple[int, str, str]:
        from mao.cli import main

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main([*self.base, *args])
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
        return code, out.getvalue(), err.getvalue()

    def fails(self, *args: str) -> tuple[int, str]:
        """返回 (退出码, 错误码)。--json 下错误是 stdout 上的 {"error": {...}}（spec/system/commands.md）。"""
        code, out, err = self(*args)
        try:
            body = json.loads(out)
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            return code, str(body["error"].get("code", ""))
        return code, err

    def json(self, *args: str):
        code, out, err = self(*args)
        assert code == 0, (args, code, err)
        return json.loads(out)

    def import_file(self, type_ref: str, name: str, text: str) -> str:
        src = self.tmp / "in" / name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(text, encoding="utf-8")
        code, out, err = self("bean", "import", "--type", type_ref, "--file", f"{name}={src}")
        assert code == 0, err
        out = out.strip()
        return json.loads(out)["id"] if out.startswith("{") else out

    def note(self, text: str = "hello factory\n") -> str:
        return self.import_file("note.text@1", "note.txt", text)

    def task(self, script: dict | None = None) -> str:
        return self.import_file("agent.task@1", "task.json", json.dumps(script or WRITE_NOTE))

    def nodes(self, run_id: str) -> dict[str, dict]:
        return {node["node_id"]: node for node in self.json("line", "nodes", run_id)}
