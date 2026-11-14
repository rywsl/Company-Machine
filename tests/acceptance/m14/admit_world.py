"""M14 验收的建库辅助（spec/system/admit.md 第 7 章）。导入本模块不会加载 mao。

在 tests/fixtures/registry-m2 的副本上增加整车刷写的例子：
- 三种类型：车辆构建记录 vehicle.build@1、固件 fw.image@1、刷写结果 vehicle.flashed@1。facts 都由 validator
  json-facts 从 JSON 载荷实测；fw.image@1 的 meta 里另有生产者自报的 target_model，用来演示 W-ADMIT-META；
- Source 工位 pull-fw@1：模拟向外部拉取指定车型、平台的固件；
- 带 admit 的刷写工位：flash@1（车型、平台一致，ECU 配额），flash-fixed@1（车型由参数指定），
  flash-many@1（列表端口里的每份固件都要与车辆同平台）；
- 两条产线：vehicle-flash@1 的接口里同时有车辆和固件；vehicle-pull-flash@1 的接口里只有车辆，固件由 Source
  步骤现拉，再与车辆在 flash 步骤汇合。

脚本经 LocalProvider 的命令映射执行，不进入测试镜像。
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

IMAGE = "mao-test/tool@sha256:09b42648636a8405354592f2ae8985670dfda5b61377ac6c75e3cebbd22e0500"

TOOLS = {
    # validator：json-facts PATH KEY...，stdout 是 {KEY: 值}；缺键或不是 JSON 对象时退出 1。
    "json-facts": """import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
except OSError:
    sys.exit(2)
except ValueError:
    print(json.dumps({"reason": "不是 JSON"}))
    sys.exit(1)
keys = sys.argv[2:]
if not isinstance(data, dict) or any(key not in data for key in keys):
    print(json.dumps({"reason": "缺少字段"}))
    sys.exit(1)
print(json.dumps({key: data[key] for key in keys}))
""",
    # Source：fw-pull MODEL PLATFORM OUT，写出一份已签名的固件描述。
    "fw-pull": """import json
import sys
from pathlib import Path

out = Path(sys.argv[3])
out.parent.mkdir(parents=True, exist_ok=True)
body = {"target_model": sys.argv[1], "platform": sys.argv[2], "signed": True, "size_kb": 2048}
out.write_text(json.dumps(body), encoding="utf-8")
""",
    # 刷写：flash-tool CAR FW OUT，产出 {vin, firmware_model}。
    "flash-tool": """import json
import sys
from pathlib import Path

car = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
fw = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
out = Path(sys.argv[3])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({"vin": car["vin"], "firmware_model": fw["target_model"]}), encoding="utf-8")
""",
}


def _validator(name: str, payload: str, keys: list[str], stem: str) -> str:
    return (
        f"  validators:\n"
        f"    - name: {name}\n"
        f"      image: {IMAGE}\n"
        f"      run: [json-facts, /bean/payload/{payload}, {', '.join(keys)}]\n"
        f"      emits: [{', '.join(keys)}]\n"
        f"      fixtures:\n"
        f"        accept: [fixtures/vehicle/{stem}-ok.json]\n"
        f"        reject: [fixtures/vehicle/{stem}-bad.json]\n"
    )


TYPES = (
    "# 整车构建记录：VIN、车型、平台、ECU 数量，都由 json-facts 实测。\n"
    'apiVersion: mao/v1\nkind: Type\nmetadata: {name: vehicle.build, version: "1.0"}\nspec:\n'
    "  payload:\n    - {path: build.json, media_type: application/json, required: true}\n"
    "  facts:\n    vin: string\n    model: string\n    platform: string\n    ecu_count: int\n"
    + _validator("build-facts", "build.json", ["vin", "model", "platform", "ecu_count"], "build")
    + "---\n"
    "# 固件：目标车型、平台、是否签名、大小（KB）由 json-facts 实测；meta.target_model 是生产者自报的。\n"
    'apiVersion: mao/v1\nkind: Type\nmetadata: {name: fw.image, version: "1.0"}\nspec:\n'
    "  payload:\n    - {path: fw.json, media_type: application/json, required: true}\n"
    "  meta_schema:\n    type: object\n    properties: {target_model: {type: string}}\n"
    "  facts:\n    target_model: string\n    platform: string\n    signed: bool\n    size_kb: int\n"
    + _validator("fw-facts", "fw.json", ["target_model", "platform", "signed", "size_kb"], "fw")
    + '  properties:\n    signed: "bean.facts.signed"\n'
    "---\n"
    "# 刷写结果：哪台车刷了哪个车型的固件。\n"
    'apiVersion: mao/v1\nkind: Type\nmetadata: {name: vehicle.flashed, version: "1.0"}\nspec:\n'
    "  payload:\n    - {path: flashed.json, media_type: application/json, required: true}\n"
    "  facts:\n    vin: string\n    firmware_model: string\n"
    + _validator("flashed-facts", "flashed.json", ["vin", "firmware_model"], "flashed")
)

FIXTURES = {
    "build-ok.json": {"vin": "LVIN0000000000001", "model": "X1", "platform": "A", "ecu_count": 4},
    "build-bad.json": {"vin": "LVIN0000000000001"},
    "fw-ok.json": {"target_model": "X1", "platform": "A", "signed": True, "size_kb": 2048},
    "fw-bad.json": {"target_model": "X1"},
    "flashed-ok.json": {"vin": "LVIN0000000000001", "firmware_model": "X1"},
    "flashed-bad.json": {"vin": "LVIN0000000000001"},
}

_CAR_FW = (
    "      inputs:  {car: {type: vehicle.build@1}, firmware: {type: fw.image@1}}\n"
    "      outputs: {flashed: {type: vehicle.flashed@1}}\n"
)
_FLASH_RUN = (
    "      run: [flash-tool, /factory/in/car/build.json, /factory/in/firmware/fw.json,"
    " /factory/out/flashed/flashed.json]\n"
)

RECIPES = (
    "    fw-pull:\n"
    "      params:  {model: {type: string}, platform: {type: string}}\n"
    "      outputs: {firmware: {type: fw.image@1, harvest: /factory/out/firmware/fw.json}}\n"
    '      run: [fw-pull, "{{ params.model }}", "{{ params.platform }}", /factory/out/firmware/fw.json]\n'
    "    flash:\n"
    + _CAR_FW
    + _FLASH_RUN
    + "    flash-fixed:\n"
    + _CAR_FW
    + "      params:  {model: {type: string}}\n"
    + _FLASH_RUN
    + "    flash-many:\n"
    "      inputs:  {car: {type: vehicle.build@1}, parts: {type: list<fw.image@1>}}\n"
    "      outputs: {flashed: {type: vehicle.flashed@1}}\n"
    "      run: [flash-tool, /factory/in/car/build.json, /factory/in/parts, /factory/out/flashed/flashed.json]\n"
)

_LIMITS = "  limits: {timeout: 60s, budget_usd: 0, retries: {rejected: 0, failed: 0, mode: fresh}}\n"


def _station(
    name: str, recipe: str, body: str, policy: str = "tool-default@1", role: str = "transform"
) -> str:
    return (
        f"apiVersion: mao/v1\nkind: Station\nmetadata: {{name: {name}, version: 1}}\nspec:\n"
        f"  role: {role}\n  machine: toolbox@1\n  recipe: {recipe}\n  operator: {{runtime: script@1}}\n"
        + body
        + _LIMITS
        + f"  policy: {policy}\n"
    )


_CAR_FW_PORTS = (
    "  inputs:\n    car:      {type: vehicle.build@1}\n    firmware: {type: fw.image@1, requires: [signed]}\n"
)
_FLASHED = "  outputs: {flashed: {type: vehicle.flashed@1}}\n"

STATIONS = {
    "pull-fw": "# Source：模拟向外部拉取指定车型、平台的固件。\n"
    + _station(
        "pull-fw",
        "fw-pull",
        "  params: {model: {type: string}, platform: {type: string}}\n  inputs: {}\n"
        "  outputs: {firmware: {type: fw.image@1, guarantees: [signed]}}\n",
        policy="source-local@1",
        role="source",
    ),
    "flash": "# 刷写：固件必须对应这台车的车型与平台，且每个 ECU 分到的固件不超过 4096 KB。\n"
    + _station(
        "flash",
        "flash",
        _CAR_FW_PORTS + "  admit:\n"
        "    - inputs.firmware.facts.target_model == inputs.car.facts.model\n"
        "    - inputs.firmware.facts.platform == inputs.car.facts.platform\n"
        "    - inputs.firmware.facts.size_kb / inputs.car.facts.ecu_count <= 4096.0\n" + _FLASHED,
    ),
    "flash-fixed": "# 专线刷写：只接受参数 model 指定的车型。\n"
    + _station(
        "flash-fixed",
        "flash-fixed",
        "  params: {model: {type: string, default: X1}}\n" + _CAR_FW_PORTS + "  admit:\n"
        "    - inputs.firmware.facts.target_model == params.model\n"
        "    - inputs.car.facts.model == params.model\n" + _FLASHED,
    ),
    "flash-many": "# 多份固件：列表里的每一份都要与车辆同平台。\n"
    + _station(
        "flash-many",
        "flash-many",
        "  inputs:\n"
        "    car:   {type: vehicle.build@1}\n"
        "    parts: {type: list<fw.image@1>}\n"
        "  admit:\n"
        "    - inputs.parts.all(p, p.facts.platform == inputs.car.facts.platform)\n"
        "    - size(inputs.parts) <= inputs.car.facts.ecu_count\n" + _FLASHED,
    ),
}

LINES = {
    "vehicle-flash": """# 接口里同时有车辆和固件（都已封存）；flash 工位的 admit 在开工前核对两者。
apiVersion: mao/v1
kind: Line
metadata: {name: vehicle-flash, version: 1}
spec:
  interface:
    inputs:
      car:      {type: vehicle.build@1}
      firmware: {type: fw.image@1, requires: [signed]}
  budget: {cap_usd: 1.0}
  steps:
    flash:
      station: flash@1
      in: {car: $inputs.car, firmware: $inputs.firmware}
  keep: [flash.flashed]
""",
    "vehicle-pull-flash": """# 接口里只有车辆；pull 步骤按车型现拉固件（平台固定为 A），再与车辆在 flash 步骤汇合。
apiVersion: mao/v1
kind: Line
metadata: {name: vehicle-pull-flash, version: 1}
spec:
  interface:
    inputs:
      car: {type: vehicle.build@1}
  budget: {cap_usd: 1.0}
  steps:
    pull:
      station: pull-fw@1
      params: {model: "{{ inputs.car.facts.model }}", platform: A}
    flash:
      station: flash@1
      in: {car: $inputs.car, firmware: pull.firmware}
  keep: [pull.firmware, flash.flashed]
""",
}


def _append(path: Path, text: str) -> None:
    body = path.read_text(encoding="utf-8").rstrip("\n") + "\n"
    path.write_text(body + text, encoding="utf-8", newline="\n")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def build_registry(dest: Path) -> Path:
    """registry-m2 的副本加上本文件的类型、配方、工位与产线。"""
    registry = dest / "registry"
    if registry.exists():
        shutil.rmtree(registry)
    shutil.copytree(FIXTURE, registry)
    _write(registry / "types" / "vehicle.yaml", TYPES)
    for name, body in FIXTURES.items():
        _write(registry / "fixtures" / "vehicle" / name, json.dumps(body))
    _append(registry / "machines" / "toolbox" / "machine.yaml", RECIPES)
    for name, text in STATIONS.items():
        _write(registry / "stations" / f"{name}.yaml", text)
    for name, text in LINES.items():
        _write(registry / "lines" / f"{name}.yaml", text)
    return registry


def commands(tmp: Path) -> dict[str, list[str]]:
    """LocalProvider 的命令映射：夹具工具加上本文件的脚本。"""
    from tests.fakes.fakelib import local_commands

    tools = tmp / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    mapping = local_commands()
    for name, text in TOOLS.items():
        script = tools / (name.replace("-", "_") + ".py")
        script.write_text(text, encoding="utf-8")
        mapping[name] = [sys.executable, str(script)]
    return mapping


def car(vin: str = "LVIN0000000000001", model: str = "X1", platform: str = "A", ecu_count: int = 4) -> str:
    return json.dumps({"vin": vin, "model": model, "platform": platform, "ecu_count": ecu_count})


def firmware(model: str = "X1", platform: str = "A", signed: bool = True, size_kb: int = 2048) -> str:
    return json.dumps({"target_model": model, "platform": platform, "signed": signed, "size_kb": size_kb})


class World:
    """进程内的工厂：LocalProvider + FakeClock + CounterIds，Runner 由 open_runner 构造。"""

    def __init__(self, tmp: Path) -> None:
        from mao.base import CounterIds, FakeClock
        from mao.context import Context
        from mao.runner import open_runner
        from mao.sandbox import LocalProvider
        from mao.snapshot import Snapshot

        self.tmp = tmp
        self.registry = build_registry(tmp)
        root = tmp / "factory"
        root.mkdir(parents=True)
        self.clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        provider = LocalProvider(root, commands=commands(tmp), clock=self.clock)
        self.ctx = Context.open(root, provider=provider, clock=self.clock, ids=CounterIds())
        self.snapshot = Snapshot.load(self.registry)
        self.runner = open_runner(self.ctx, self.snapshot)
        self.cli = Cli(tmp, world=self)

    def key(self, ref: str) -> str:
        return self.snapshot.key("Station", ref)

    def car(self, **kw) -> str:
        return self.cli.import_file("vehicle.build@1", "build.json", car(**kw))

    def firmware(self, **kw) -> str:
        return self.cli.import_file("fw.image@1", "fw.json", firmware(**kw))

    def jobs(self) -> int:
        return int(self.ctx.db.query("SELECT COUNT(*) AS n FROM jobs")[0]["n"])

    def submit(self, station: str, inputs: dict, params: dict | None = None):
        return self.runner.submit(self.key(station), inputs, params or {})


class Cli:
    """经 `factory` 命令行（mao.cli.main）操作工厂目录；LocalProvider，命令映射写成文件。"""

    def __init__(self, tmp: Path, *, world: World | None = None) -> None:
        self.tmp = tmp
        if world is None:
            self.registry, self.root = build_registry(tmp), tmp / "factory"
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

    def json(self, *args: str, expect: int = 0):
        code, out, err = self(*args)
        assert code == expect, (args, code, out, err)
        return json.loads(out)

    def import_file(self, type_ref: str, name: str, text: str) -> str:
        src = self.tmp / "in" / name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(text, encoding="utf-8")
        code, out, err = self("bean", "import", "--type", type_ref, "--file", f"{name}={src}")
        assert code == 0, (out, err)
        out = out.strip()
        return json.loads(out)["id"] if out.startswith("{") else out

    def nodes(self, run_id: str) -> dict[str, dict]:
        return {node["node_id"]: node for node in self.json("line", "nodes", run_id)}


def registry_check(registry: Path, *flags: str) -> tuple[int, dict]:
    """经 `mrl registry check ROOT --json`（mrl.cli.main）检查 Registry，返回（退出码、JSON 结果）。"""
    from mrl.cli import main

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(["registry", "check", str(registry), "--json", *flags])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, json.loads(out.getvalue())
