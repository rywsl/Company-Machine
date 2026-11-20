"""M12 验收的夹具（spec/system/instances.md、ingress.md 第 7 章）。导入本模块不会加载 mao。

在 tests/fixtures/registry-m2 的副本上增加：
- poolbox@1 一族（pooled，reset 成功、失败、缺失、pool_size 2）；
- residentbox@1（resident，主进程 res-main 读收件目录、经 factoryctl emit 封存、按指令退出）；
- hookbox@1（once，只为声明入站路由的工位提供配方）；
- 对应的工位与策略。
机器主进程与配方命令都是测试运行时写出的标准库脚本，经 LocalProvider 的命令映射执行。
Registry 是 Git 仓库，带治理。时钟是从当前时间开始的 FakeClock。
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tests.acceptance.m3.gov_world import FIXTURE, git
from tests.acceptance.m5.auto_world import _patch_runtimes, foreman_section

IMAGE = "mao-test/tool@sha256:09b42648636a8405354592f2ae8985670dfda5b61377ac6c75e3cebbd22e0500"

_HEAD = """import json
import os
import subprocess
import sys
import time
from pathlib import Path


def factoryctl(*args):
    done = subprocess.run(
        [sys.executable, os.environ["FACTORYCTL"], *args], capture_output=True, text=True, encoding="utf-8"
    )
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def emit(body):
    out = Path(os.environ["FACTORY_OUT"]) / "note"
    out.mkdir(parents=True, exist_ok=True)
    (out / "note.txt").write_text(json.dumps(body, sort_keys=True) + "\\n", encoding="utf-8")
"""

TOOLS = {
    # pooled 配方：记录实例、残留、reset 次数、输入与 ctl 往返
    "pool-work": _HEAD
    + """
out = Path(os.environ["FACTORY_OUT"])
residue = sorted(p.name for p in out.iterdir()) if out.is_dir() else []
ws = Path(os.environ["FACTORY_WORKSPACE"])
log = ws / "reset.log"
resets = log.read_text(encoding="utf-8").count("\\n") if log.exists() else 0
code, stdout, _stderr = factoryctl("approval", "request", "--kind", "probe", "--summary", "pooled ctl")
try:
    decision = json.loads(stdout).get("decision")
except ValueError:
    decision = None
note = (Path(os.environ["FACTORY_IN"]) / "note" / "note.txt").read_text(encoding="utf-8")
inputs = sorted(p.name for p in Path(os.environ["FACTORY_IN"]).iterdir())
seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
if seconds:
    time.sleep(seconds)
emit({"instance": os.environ.get("FACTORY_INSTANCE", ""), "job": os.environ.get("FACTORY_JOB_ID", ""),
      "residue": residue, "resets": resets, "ctl_exit": code, "decision": decision, "input": note,
      "inputs": inputs})
""",
    "pool-reset": """import os
from pathlib import Path
ws = Path(os.environ["FACTORY_WORKSPACE"])
ws.mkdir(parents=True, exist_ok=True)
with (ws / "reset.log").open("a", encoding="utf-8") as f:
    f.write("reset\\n")
""",
    "pool-reset-fail": "import sys\nsys.exit(3)\n",
    # resident 主进程：启动计数写进 /factory/state；处理收件目录里的指令
    "res-main": _HEAD
    + """
import base64
ctl = Path(os.environ["FACTORY_INSTANCE_CTL"])
base = ctl.parent
state = Path(os.environ["FACTORY_STATE"])
state.mkdir(parents=True, exist_ok=True)
count = state / "count"
n = int(count.read_text(encoding="utf-8")) + 1 if count.exists() else 1
count.write_text(str(n), encoding="utf-8")
(state / "main.txt").write_text(f"started {n}", encoding="utf-8")
inbox, outdir, results = base / "inbox", base / "out", base / "results"
results.mkdir(parents=True, exist_ok=True)
while True:
    for item in sorted(inbox.glob("*.json")) if inbox.is_dir() else []:
        body = json.loads(item.read_text(encoding="utf-8"))
        name = item.stem
        if body.get("exit") is not None:
            (results / f"{name}.json").write_text(json.dumps({"exit": body["exit"]}), encoding="utf-8")
            item.unlink()
            sys.exit(int(body["exit"]))
        if "body_b64" in body:
            text = base64.b64decode(body["body_b64"]).decode("utf-8")
        else:
            text = body.get("text", "")
        folder = Path(body["dir"]) if body.get("dir") else outdir / name
        folder.mkdir(parents=True, exist_ok=True)
        if text:
            (folder / "note.txt").write_text(text, encoding="utf-8")
        args = ["emit", "--port", body.get("port", "note"), "--dir", str(folder)]
        for key, value in sorted((body.get("labels") or {}).items()):
            args += ["--label", f"{key}={value}"]
        code, stdout, stderr = factoryctl(*args)
        (results / f"{name}.json").write_text(
            json.dumps({"code": code, "stdout": stdout, "stderr": stderr, "route": body.get("route"), "raw": body}),
            encoding="utf-8",
        )
        item.unlink()
    time.sleep(0.05)
""",
    # resident 工位的 job：读主进程写的状态
    "res-reply": _HEAD
    + """
state = Path(os.environ.get("FACTORY_STATE", "/factory/state"))
text = (state / "main.txt").read_text(encoding="utf-8") if (state / "main.txt").exists() else ""
emit({"state": text, "instance": os.environ.get("FACTORY_INSTANCE", "")})
""",
}

_NOTE = "{note: {type: note.text@1}}"


def _machine(name: str, runtime: str, recipes: str) -> str:
    return f"""apiVersion: mao/v1
kind: Machine
metadata: {{name: {name}, version: 1}}
spec:
  form: tool
  runtime:
    image: {IMAGE}
{runtime}  needs: {{egress: none}}
  recipes:
{recipes}"""


_WORK = f"""    work:
      inputs:  {_NOTE}
      params:  {{sleep: {{type: string, default: "0"}}}}
      outputs: {_NOTE}
      run: [pool-work, "{{{{ params.sleep }}}}"]
"""
_RES_RECIPES = f"""    listen:
      inputs:  {{}}
      outputs: {_NOTE}
      run: [res-reply]
    reply:
      inputs:  {_NOTE}
      outputs: {_NOTE}
      run: [res-reply]
"""
_HOOK_RECIPES = f"""    in:
      inputs:  {{}}
      outputs: {_NOTE}
      run: [res-reply]
    relay:
      inputs:  {_NOTE}
      outputs: {_NOTE}
      run: [res-reply]
"""

MACHINES = {
    "poolbox": _machine("poolbox", "    instance: pooled\n    reset: [pool-reset]\n", _WORK),
    "poolbox2": _machine(
        "poolbox2", "    instance: pooled\n    pool_size: 2\n    reset: [pool-reset]\n", _WORK
    ),
    "poolfail": _machine("poolfail", "    instance: pooled\n    reset: [pool-reset-fail]\n", _WORK),
    "poolbad": _machine("poolbad", "    instance: pooled\n", _WORK),
    "residentbox": _machine(
        "residentbox",
        "    instance: resident\n    command: [res-main]\n    restart: {max: 3}\n    emit_per_min: 3\n",
        _RES_RECIPES,
    ),
    "hookbox": _machine("hookbox", "    instance: once\n", _HOOK_RECIPES),
}

POLICIES = {
    "inst-none": (
        "apiVersion: mao/v1\nkind: Policy\nmetadata: {name: inst-none, version: 1}\n"
        "spec:\n  applies_to: [transform, source]\n  egress: {none: true}\n  secrets: []\n"
    ),
}


def _station(
    name: str,
    machine: str,
    recipe: str,
    *,
    role: str = "transform",
    timeout: str = "60s",
    extra: str = "",
    version: int = 1,
) -> str:
    inputs = "{}" if recipe in ("listen", "in") else _NOTE
    params = '  params: {sleep: {type: string, default: "0"}}\n' if recipe == "work" else ""
    if recipe == "work":
        extra = (
            '  approval:\n    in_run: {auto_if: "request.cost_usd <= 0.5", else: foreman}\n'
            "    timeout: 10m\n" + extra
        )
    return f"""apiVersion: mao/v1
kind: Station
metadata: {{name: {name}, version: {version}}}
spec:
  role: {role}
  machine: {machine}
  recipe: {recipe}
  operator: {{runtime: script@1}}
{params}  inputs:  {inputs}
  outputs: {_NOTE}
{extra}  limits: {{timeout: {timeout}, budget_usd: 0, retries: {{rejected: 0, failed: 0, mode: fresh}}}}
  policy: inst-none@1
"""


def _route(name: str, body: str) -> str:
    return f"  ingress:\n    {name}: {body}\n"


STATIONS = {
    "pool-work": _station("pool-work", "poolbox@1", "work"),
    "pool-slow": _station("pool-slow", "poolbox@1", "work", timeout="2s"),
    "pool-two": _station("pool-two", "poolbox2@1", "work"),
    "pool-fail": _station("pool-fail", "poolfail@1", "work"),
    "pool-bad": _station("pool-bad", "poolbad@1", "work"),
    "res-listen": _station(
        "res-listen",
        "residentbox@1",
        "listen",
        role="source",
        extra=_route("res-drop", "{auth: {kind: bearer, secret: RES_TOKEN}, deliver: instance}"),
    ),
    "res-reply": _station("res-reply", "residentbox@1", "reply"),
    "hook-bean": _station(
        "hook-bean",
        "hookbox@1",
        "in",
        role="source",
        extra=(
            "  ingress:\n"
            "    note-push: {auth: {kind: bearer, secret: NOTE_TOKEN}, deliver: bean, port: note}\n"
            "    note-sign: {auth: {kind: hmac-sha256, secret: SIGN_TOKEN, header: X-Signature},"
            " deliver: bean, port: note}\n"
            "    note-nokey: {auth: {kind: bearer, secret: MISSING_TOKEN}, deliver: bean, port: note}\n"
            "    note-wrongfile: {auth: {kind: bearer, secret: NOTE_TOKEN}, deliver: bean, port: note,"
            " file: wrong.txt}\n"
        ),
    ),
    "hook-dup-a": _station(
        "hook-dup-a",
        "hookbox@1",
        "in",
        role="source",
        extra=_route("dup", "{auth: {kind: bearer, secret: NOTE_TOKEN}, deliver: bean, port: note}"),
    ),
    "hook-dup-b": _station(
        "hook-dup-b",
        "hookbox@1",
        "in",
        role="source",
        extra=_route("dup", "{auth: {kind: bearer, secret: NOTE_TOKEN}, deliver: bean, port: note}"),
    ),
    "hook-role": _station(
        "hook-role",
        "hookbox@1",
        "relay",
        extra=_route("bad-role", "{auth: {kind: bearer, secret: NOTE_TOKEN}, deliver: bean, port: note}"),
    ),
}

SECRET_VALUES = {
    "NOTE_TOKEN": "note-token-value-7f3a",
    "SIGN_TOKEN": "sign-token-value-91be",
    "RES_TOKEN": "res-token-value-c24d",
}


def write_registry(repo: Path) -> None:
    for name, text in MACHINES.items():
        path = repo / "machines" / name / "machine.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    for name, text in POLICIES.items():
        (repo / "policies" / f"{name}.yaml").write_text(text, encoding="utf-8", newline="\n")
    for name, text in STATIONS.items():
        (repo / "stations" / f"{name}.yaml").write_text(text, encoding="utf-8", newline="\n")


def commands(tmp: Path) -> dict[str, list[str]]:
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
    def __init__(
        self,
        tmp: Path,
        *,
        instances: dict[str, Any] | None = None,
        ingress: dict[str, Any] | None = None,
        secrets: bool = True,
    ) -> None:
        from mao.base import CounterIds, FakeClock
        from mao.context import Context
        from mao.governance import EpochStore
        from mao.runner import open_runner
        from mao.sandbox import LocalProvider

        self.tmp = tmp
        self.repo = tmp / "registry"
        shutil.copytree(FIXTURE, self.repo)
        _patch_runtimes(self.repo)
        write_registry(self.repo)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "core.autocrlf", "false")
        git(self.repo, "checkout", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "initial registry")
        self.base = git(self.repo, "rev-parse", "HEAD")

        self.mapping = commands(tmp)
        script = tmp / "agent-script.json"
        script.write_text(json.dumps({"default": []}), encoding="utf-8")
        self.root = tmp / "factory"
        self.root.mkdir()
        config: dict[str, Any] = {
            "governance": {"registry": str(self.repo), "git_timeout_s": 60},
            "foreman": foreman_section(script),
            "instances": instances
            or {
                "health_every_s": 1,
                "idle_ttl_s": 600,
                "reset_timeout_s": 30,
                "backoff_base_s": 5,
                "backoff_max_s": 600,
            },
        }
        if ingress is not None:
            config["ingress"] = ingress
        self.write_config(config)
        if secrets:
            for name, value in SECRET_VALUES.items():
                self.put_secret(name, "other", value)

        self.clock = FakeClock(datetime.now(UTC).replace(microsecond=0))
        provider = LocalProvider(self.root, commands=self.mapping, clock=self.clock)
        self.ctx = Context.open(self.root, provider=provider, clock=self.clock, ids=CounterIds())
        self.epochs = EpochStore(self.ctx)
        self.epochs.init(self.base, by="human:acceptance")
        self.snapshot = self.epochs.materialize(self.base)
        self.runner = open_runner(self.ctx, self.snapshot)
        self.instances = self.runner.instances

    # ------------------------------------------------------------ 配置与秘密

    def write_config(self, config: dict[str, Any]) -> None:
        self.config = config
        (self.root / "factory.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def put_secret(self, name: str, klass: str, value: str) -> None:
        folder = self.root / "secrets"
        (folder / "values").mkdir(parents=True, exist_ok=True)
        manifest = folder / "manifest.json"
        body = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
        body[name] = klass
        manifest.write_text(json.dumps(body), encoding="utf-8")
        (folder / "values" / name).write_text(value, encoding="utf-8", newline="")

    # ------------------------------------------------------------ 运行

    def key(self, ref: str) -> str:
        return self.snapshot.key("Station", ref if "@" in ref else ref + "@1")

    def note(self, text: str = "hello factory\n") -> str:
        from mao.warehouse import BeanDraft

        type_id = self.snapshot.type_key("note.text@1").split(":", 1)[1]
        incoming = self.root / "incoming" / self.ctx.ids.new("in")
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

    def submit(
        self, station: str, params: dict[str, Any] | None = None, *, text: str = "hello factory\n"
    ) -> str:
        return self.runner.submit(self.key(station), {"note": self.note(text)}, params or {}).id

    def run(self, station: str, params: dict[str, Any] | None = None, *, text: str = "hello factory\n"):
        return self.runner.run(self.submit(station, params, text=text))

    def output(self, job) -> dict[str, Any]:
        found = job.outputs["note"]
        return self.bean_json(found[0] if isinstance(found, list) else found)

    def bean_json(self, bean_id: str) -> Any:
        return json.loads(self.bean_text(bean_id))

    def bean_text(self, bean_id: str) -> str:
        bean = self.ctx.warehouse.get(bean_id)
        [ref] = [item for item in bean.payload if item.path.endswith("note.txt")]
        return self.ctx.warehouse.blob_path(ref.digest).read_text(encoding="utf-8")

    def events(self, kind: str) -> list[dict[str, Any]]:
        rows = self.ctx.db.query("SELECT data FROM events WHERE kind = ? ORDER BY seq", (kind,))
        return [json.loads(row["data"]) for row in rows]

    def rows(self, sql: str, args: tuple = ()) -> list[Any]:
        return list(self.ctx.db.query(sql, args))

    def set_state(self, job_ids: list[str], state: str) -> None:
        with self.ctx.db.tx() as conn:
            for job_id in job_ids:
                conn.execute("UPDATE jobs SET state = ? WHERE id = ?", (state, job_id))

    def scheduler(self, snapshot: Any = None):
        from mao.scheduler import Scheduler

        (self.root / "scheduler.json").write_text(json.dumps(SCHEDULER), encoding="utf-8")
        scheduler = Scheduler(self.ctx, snapshot or self.snapshot, self.runner, instances=self.instances)
        scheduler.load_config()
        return scheduler

    # ------------------------------------------------------------ 实例

    def instance(self, station: str, *, state: str | None = None):
        found = [
            r for r in self.instances.list(station=self.key(station)) if state is None or r.state == state
        ]
        return found[-1] if found else None

    def live(self, station: str):
        found = [r for r in self.instances.list(station=self.key(station)) if r.state not in ("stopped",)]
        return found[-1] if found else None

    def main_dir(self, instance_id: str) -> Path:
        return self.root / "instances" / instance_id / "main"

    def drop(self, instance_id: str, name: str, body: dict[str, Any]) -> None:
        """往实例的收件目录放一条指令（先写 .tmp 再改名）。"""
        inbox = self.main_dir(instance_id) / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        tmp = inbox / f"{name}.tmp"
        tmp.write_text(json.dumps(body), encoding="utf-8")
        tmp.replace(inbox / f"{name}.json")

    def result(self, instance_id: str, name: str, timeout: float = 60.0) -> dict[str, Any]:
        path = self.main_dir(instance_id) / "results" / f"{name}.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.is_file():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except ValueError:
                    pass
            time.sleep(0.05)
        raise AssertionError(f"主进程没有处理 {name}")

    def until(self, predicate, *, step_s: float = 1.0, timeout: float = 60.0, snapshot: Any = None) -> None:
        """推进时钟并 reconcile，直到 predicate() 为真。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.clock.advance(step_s)
            self.instances.reconcile(snapshot)
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError("条件没有满足")

    def wait_text(self, path: Path, timeout: float = 60.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                if text:
                    return text
            time.sleep(0.05)
        raise AssertionError(f"{path} 没有出现")

    def commit(self, edits: dict[str, str], message: str):
        """改 Registry 并让新提交成为 active，返回新快照。"""
        for rel, text in edits.items():
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        head = git(self.repo, "rev-parse", "HEAD")
        self.epochs.build(head)
        self.epochs.set_pointer("active", head, by="human:acceptance", reason="m12 acceptance")
        return self.epochs.materialize(head)

    def close(self) -> None:
        for record in self.instances.list():
            if record.state != "stopped":
                try:
                    self.instances.stop(record.id)
                except Exception:  # noqa: BLE001 — 清理尽力而为
                    pass

    def cli(self, *args: str) -> tuple[int, Any, str]:
        from mao.cli import main

        mapping = self.tmp / "commands.json"
        mapping.write_text(json.dumps(self.mapping), encoding="utf-8")
        base = ["--root", str(self.root), "--provider", "local", "--local-commands", str(mapping), "--json"]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main([*base, *args])
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
        text = out.getvalue()
        try:
            body: Any = json.loads(text)
        except ValueError:
            body = text
        return code, body, err.getvalue()


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


def serve_config(**overrides: Any):
    from mao.daemon import ServeConfig

    values: dict[str, Any] = {
        "tick_s": 0.0,
        "max_parallel_jobs": 4,
        "audit_every_s": None,
        "canary_every_s": None,
        "docker_timeout_s": 60.0,
        "supervise": False,
    }
    values.update(overrides)
    return ServeConfig(**values)


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")
