"""M2 验收测试的建库辅助。导入本模块不会加载 mao。"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "spec" / "examples" / "registry"
FIXTURE = ROOT / "tests" / "fixtures" / "registry-m2"
LINE = "explainer-multilang@5"
_REQUIRED = (
    "lines/note-pipeline.yaml",
    "lines/note-dead.yaml",
    "lines/note-trigger.yaml",
    "stations/publish-local.yaml",
    "stations/publish-flaky.yaml",
    "stations/publish-templated.yaml",
    "stations/pull-notes.yaml",
    "stations/quote.yaml",
    "stations/publish-staged.yaml",
)
_missing = [name for name in _REQUIRED if not (FIXTURE / name).is_file()]
if not FIXTURE.is_dir() or _missing:
    raise FileNotFoundError(f"registry-m2 fixture is missing: {FIXTURE} {_missing}")


def copy_example(dest: Path) -> Path:
    registry = dest / "registry"
    if registry.exists():
        shutil.rmtree(registry)
    shutil.copytree(EXAMPLE, registry)
    return registry


def patch_vtt(dest: Path) -> Path:
    """演进场景 13.2：explainer 字幕改为 VTT，配方输出一并改，Registry 仍零错误。"""
    registry = copy_example(dest)
    station = registry / "stations" / "explainer.yaml"
    station.write_text(
        station.read_text(encoding="utf-8").replace(
            "subtitle: {type: subtitle.srt@1, optional: false}",
            "subtitle: {type: subtitle.vtt@1, optional: false}",
        ),
        encoding="utf-8",
        newline="\n",
    )
    machine = registry / "machines" / "openmontage" / "machine.yaml"
    old = 'subtitle: {type: subtitle.srt@1, harvest: "projects/*/renders/*.srt", optional: true}'
    new = 'subtitle: {type: subtitle.vtt@1, harvest: "projects/*/renders/*.vtt", optional: true}'
    text = machine.read_text(encoding="utf-8")
    if old not in text:
        raise AssertionError("explainer recipe subtitle line missing")
    machine.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")
    return registry


def open_factory(tmp: Path, registry: Path):
    from mao.base import CounterIds, FakeClock
    from mao.context import Context
    from mao.runner import Runner
    from mao.sandbox import LocalProvider
    from mao.snapshot import Snapshot
    from tests.fakes.fakelib import local_commands

    root = tmp / "factory"
    root.mkdir()
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    provider = LocalProvider(root, commands=local_commands(), clock=clock)
    ctx = Context.open(root, provider=provider, clock=clock, ids=CounterIds())
    snapshot = Snapshot.load(registry)
    return ctx, snapshot, Runner(ctx, snapshot), clock


def line_spec(snapshot, line_ref: str) -> dict:
    return snapshot.spec(snapshot.key("Line", line_ref))


def seal_input(ctx, snapshot, type_ref: str) -> str:
    from mao.warehouse import BeanDraft

    type_key = snapshot.type_key(type_ref)
    type_id = type_key.split(":", 1)[1]
    spec = snapshot.spec(type_key)
    files = {}
    incoming = ctx.root / "incoming"
    for item in spec.get("payload") or [{"path": "payload.bin"}]:
        rel = str(item.get("path", "payload.bin")).replace("*", "item.txt")
        path = incoming / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"acceptance\n")
        files[rel] = path
    facts = {}
    for name, typ in snapshot.facts_decl(type_id).items():
        facts[name] = {"int": 1, "float": 1.0, "string": "x", "bool": True}.get(typ, 1)
    bean = ctx.warehouse.add(BeanDraft(type=type_id, meta={}, files=files))
    ctx.warehouse.seal_many([(bean.id, facts)], job_id=None, report_digest="sha256:" + "0" * 64)
    sealed = ctx.warehouse.get(bean.id)
    if not sealed.sealed:
        raise AssertionError("seal_many did not seal")
    return bean.id


def start_line(tmp, line_ref: str, registry=None):
    from mao.runs import LineRunner

    registry = registry or FIXTURE
    ctx, snapshot, runner, clock = open_factory(tmp, registry)
    spec = line_spec(snapshot, line_ref)
    inputs = {}
    for name, decl in (spec.get("interface") or {}).get("inputs", {}).items():
        type_ref = decl["type"] if isinstance(decl, dict) else decl
        inputs[name] = seal_input(ctx, snapshot, type_ref)
    lines = LineRunner(ctx, snapshot, runner, cache=None)
    return ctx, snapshot, runner, clock, lines, inputs
