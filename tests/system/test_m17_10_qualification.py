"""M17-10 资格：真实子进程适配器、产品身份和地址恢复。"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from mao.base import CounterIds, FakeClock
from mao.context import Context
from mao.errors import FactoryError
from mao.governance.epochs import EpochStore
from mao.qualification import qualify_machine, qualify_runtime, receipt_valid
from mao.sandbox import LocalProvider
from tests.acceptance.m3.gov_world import FIXTURE, START, git
from tests.fakes.fakelib import local_commands

_ADAPTER = r"""
import json, os, re, sys
from pathlib import Path
args = sys.argv[1:]

def take(flag):
    if flag not in args:
        return ""
    return args[args.index(flag) + 1]

prompt = take("-p")
contract = take("--append-system-prompt")
resume = take("--resume")
fmt = take("--format") or "stream-json"
mode = os.environ.get("QUAL_MODE", "good")
fence = "" if mode == "no-fence" else os.environ.get("QUAL_FENCE", "")
home = Path(os.environ["QUAL_HOME"])
home.mkdir(parents=True, exist_ok=True)
session = resume or "sess-" + os.environ.get("QUAL_FENCE", "x")[-6:]
memory = home / (session + ".json")
token = ""
match = re.search(r"包含 (\S+)", contract)
if match:
    token = match.group(1)

def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")

if resume:
    if mode == "no-recall" or not memory.is_file():
        emit({"type": "result", "is_error": True, "result": "", "session_id": session, "num_turns": 1})
        sys.exit(1)
    fact = json.loads(memory.read_text(encoding="utf-8"))["fact"]
    emit({"type": "result", "is_error": False, "result": fact, "session_id": session, "num_turns": 1, "total_cost_usd": 0})
    sys.exit(0)

found = re.search(r"记住私有事实 (\S+)", prompt)
fact = found.group(1) if found else ""
memory.write_text(json.dumps({"fact": fact}), encoding="utf-8")
call = {"id": "toolu_1", "name": "echo", "fence": fence, "operationId": "op1"}
if fmt == "codex-jsonl":
    emit({"type": "tool_call", **call})
else:
    emit({"type": "assistant", "tool_use": call, "session_id": session})
emit({"type": "tool_result", "tool_use_id": "toolu_1", "call_id": "toolu_1"})
if mode == "duplicate":
    emit({"type": "tool_call", **call})
    emit({"type": "tool_result", "tool_use_id": "toolu_1"})
result = "" if mode == "empty-result" else ("ok" if mode == "ignore-contract" else token)
turns = 9 if mode == "over-turns" else 1
emit({"type": "result", "is_error": False, "result": result, "session_id": session, "num_turns": turns, "total_cost_usd": 0.01})
"""

_MACHINE = r"""
import json, sys
from pathlib import Path
args = sys.argv[1:]

def take(flag, default=""):
    if flag not in args:
        return default
    return args[args.index(flag) + 1]

path = Path(take("--self-check"))
mode = take("--mode", "sync")
declared = take("--commit")
image = take("--image")
poll = take("--poll")
state_path = path.with_suffix(".count")
count = int(state_path.read_text(encoding="utf-8")) if state_path.is_file() else 0
if not poll:
    count += 1
    state_path.write_text(str(count), encoding="utf-8")
    if mode == "async":
        path.write_text(json.dumps({"state": "pending", "task_id": "task-1", "identity": {"commit": declared, "image": image}}), encoding="utf-8")
        sys.exit(0)
commit = "stub-script" if mode == "stub" else declared
body = {
    "state": "done",
    "healthy": True,
    "invoked": True,
    "artifact": "note-sealed",
    "identity": {"commit": commit, "image": image},
    "recovery": {"resubmit": count > 1, "task_id": "task-sync"},
    "base_url": "http://127.0.0.1:9",
    "allowed_origins": ["http://127.0.0.1:9"],
    "location": "/files/note.txt",
    "redirects": ["http://127.0.0.1:9/files/note.txt"] if mode != "bad-url" else ["file:///tmp/note.txt"],
}
path.write_text(json.dumps(body), encoding="utf-8")
"""


def test_real_cli_session_contract_and_negative_adapters(tmp_path: Path) -> None:
    world = _world(tmp_path)
    good = qualify_runtime(world.ctx, world.key("qual-good"), profile="offline")
    assert good["status"] == "passed"
    assert {item["id"] for item in good["checks"] if item["passed"]} >= {
        "launch",
        "contract",
        "resume",
        "result",
    }
    ignored = _fail_runtime(world, "qual-ignore")
    assert "contract" in ignored
    blank = _fail_runtime(world, "qual-empty")
    assert "result" in blank
    forgotten = _fail_runtime(world, "qual-forget")
    assert "resume" in forgotten


def test_tool_ids_duplicates_errors_and_turn_limit(tmp_path: Path) -> None:
    world = _world(tmp_path)
    good = qualify_runtime(world.ctx, world.key("qual-good"), profile="offline")
    assert _check(good, "tools") and _check(good, "limits") and _check(good, "fencing")
    assert "tools" in _fail_runtime(world, "qual-duplicate")
    assert "limits" in _fail_runtime(world, "qual-turns")
    assert "fencing" in _fail_runtime(world, "qual-fence")


def test_two_runtime_adapters_share_protocol(tmp_path: Path) -> None:
    world = _world(tmp_path)
    stream = qualify_runtime(world.ctx, world.key("qual-good"), profile="offline")
    codex = qualify_runtime(world.ctx, world.key("qual-codex"), profile="offline")
    assert stream["status"] == codex["status"] == "passed"
    assert stream["environment"]["transcript"] == "stream-json"
    assert codex["environment"]["transcript"] == "codex-jsonl"
    assert stream["subject_key"] != codex["subject_key"]


def test_machine_identity_cannot_be_replaced_by_stub(tmp_path: Path) -> None:
    world = _world(tmp_path)
    notes = qualify_machine(world.ctx, world.key("notes-tool"), profile="offline")
    assert notes["status"] == "passed"
    assert notes["environment"]["form"] == "tool"
    try:
        qualify_machine(world.ctx, world.key("openmontage-stub"), profile="offline")
    except FactoryError as exc:
        assert exc.code == "MACHINE_IDENTITY_MISMATCH"
    else:
        raise AssertionError("stub passed identity")


def test_service_relative_absolute_redirect_and_integrity(tmp_path: Path) -> None:
    world = _world(tmp_path)
    service = qualify_machine(world.ctx, world.key("notes-service"), profile="offline")
    assert _check(service, "network") and _check(service, "recovery")
    try:
        qualify_machine(world.ctx, world.key("notes-bad-url"), profile="offline")
    except FactoryError as exc:
        assert exc.code == "MACHINE_IDENTITY_MISMATCH" or "network" in exc.detail.get("checks", [])
    else:
        raise AssertionError("file redirect was accepted")


def test_sync_and_async_nonmedia_operations(tmp_path: Path) -> None:
    world = _world(tmp_path)
    sync = qualify_machine(world.ctx, world.key("notes-tool"), profile="offline")
    async_notes = qualify_machine(world.ctx, world.key("notes-service"), profile="offline")
    assert sync["environment"]["form"] == "tool"
    assert async_notes["environment"]["form"] == "service"
    assert _check(async_notes, "recovery")
    assert "video" not in json.dumps(sync["environment"])


def test_qualification_invalidates_on_version_change(tmp_path: Path) -> None:
    world = _world(tmp_path)
    receipt = qualify_runtime(world.ctx, world.key("qual-good"), profile="offline")
    assert receipt_valid(world.ctx, receipt)
    assert receipt["profile"] == "offline"
    script = world.adapter
    script.write_text(script.read_text(encoding="utf-8") + "\n# version-2\n", encoding="utf-8")
    assert receipt_valid(world.ctx, receipt) is False


class _World:
    def __init__(self, ctx, epochs, adapter: Path) -> None:
        self.ctx = ctx
        self.epochs = epochs
        self.adapter = adapter
        self.snapshot = epochs.materialize(epochs.current())

    def key(self, name: str) -> str:
        kind = "OperatorRuntime" if name.startswith("qual-") else "Machine"
        return self.snapshot.key(kind, name + "@1")


def _world(tmp_path: Path) -> _World:
    adapter = tmp_path / "adapter.py"
    machine = tmp_path / "machine.py"
    adapter.write_text(_ADAPTER, encoding="utf-8")
    machine.write_text(_MACHINE, encoding="utf-8")
    repo = tmp_path / "registry"
    shutil.copytree(FIXTURE, repo)
    py = Path(sys.executable).as_posix()
    adapter_s = adapter.as_posix()
    machine_s = machine.as_posix()
    (repo / "runtimes").mkdir(exist_ok=True)
    for name, mode, fmt in (
        ("qual-good", "good", "stream-json"),
        ("qual-codex", "good", "codex-jsonl"),
        ("qual-ignore", "ignore-contract", "stream-json"),
        ("qual-empty", "empty-result", "stream-json"),
        ("qual-forget", "no-recall", "stream-json"),
        ("qual-duplicate", "duplicate", "stream-json"),
        ("qual-turns", "over-turns", "stream-json"),
        ("qual-fence", "no-fence", "stream-json"),
    ):
        (repo / "runtimes" / f"{name}.yaml").write_text(
            _runtime_yaml(name, py, adapter_s, mode, fmt), encoding="utf-8"
        )
    for name, form, mode, commit, image in (
        ("notes-tool", "tool", "sync", "a" * 40, "mao-test/notes@sha256:" + "ab" * 32),
        ("notes-service", "service", "async", "b" * 40, "mao-test/notes@sha256:" + "cd" * 32),
        ("notes-bad-url", "service", "bad-url", "c" * 40, "mao-test/notes@sha256:" + "ef" * 32),
        ("openmontage-stub", "tool", "stub", "d" * 40, "mao-test/openmontage@sha256:" + "ff" * 32),
    ):
        folder = repo / "machines" / name
        folder.mkdir(parents=True)
        (folder / "machine.yaml").write_text(
            _machine_yaml(name, form, py, machine_s, mode, commit, image),
            encoding="utf-8",
        )
    git(repo, "init", "-q")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "checkout", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "qualification registry")
    base = git(repo, "rev-parse", "HEAD")
    root = tmp_path / "factory"
    root.mkdir()
    (root / "factory.json").write_text(
        json.dumps({"governance": {"registry": str(repo), "git_timeout_s": 60}}),
        encoding="utf-8",
    )
    ctx = Context.open(
        root,
        provider=LocalProvider(root, commands=local_commands()),
        clock=FakeClock(START),
        ids=CounterIds(),
    )
    epochs = EpochStore(ctx)
    epochs.init(base, by="human:acceptance")
    return _World(ctx, epochs, adapter)


def _q(value: str) -> str:
    return json.dumps(value)


def _runtime_yaml(name: str, py: str, adapter: str, mode: str, fmt: str) -> str:
    argv = (
        f'[{_q(py)}, {_q(adapter)}, --format, {_q(fmt)}, -p, "{{{{ brief.task }}}}", '
        f'--append-system-prompt, "{{{{ brief.factory_contract }}}}", '
        f'--max-turns, "{{{{ limits.max_turns }}}}"]'
    )
    resume = (
        f'[{_q(py)}, {_q(adapter)}, --format, {_q(fmt)}, --resume, "{{{{ session.id }}}}", '
        f'-p, "{{{{ brief.task }}}}", --append-system-prompt, "{{{{ brief.factory_contract }}}}"]'
    )
    return (
        "apiVersion: mao/v1\n"
        "kind: OperatorRuntime\n"
        f"metadata: {{name: {name}, version: 1}}\n"
        "spec:\n"
        "  kind: agent-cli\n"
        "  invoke:\n"
        f"    argv: {argv}\n"
        f"    resume: {resume}\n"
        f"  env: {{QUAL_MODE: {mode}}}\n"
        f"  transcript: {{format: {fmt}, from: stdout}}\n"
        "  models_allowed: [fake-model-1]\n"
    )


def _machine_yaml(name: str, form: str, py: str, script: str, mode: str, commit: str, image: str) -> str:
    return (
        "apiVersion: mao/v1\n"
        "kind: Machine\n"
        f"metadata: {{name: {name}, version: 1}}\n"
        "spec:\n"
        f"  form: {form}\n"
        f"  source: {{repo: {name}, commit: {commit}}}\n"
        "  runtime:\n"
        f"    image: {image}\n"
        f"    command: [{_q(py)}, {_q(script)}, --commit, {_q(commit)}, --image, {_q(image)}, --mode, {_q(mode)}]\n"
        "  recipes:\n"
        "    note:\n"
        "      inputs: {note: {type: note.text@1}}\n"
        "      outputs: {note: {type: note.text@1}}\n"
        "      run: [upper-text, /factory/in/note/note.txt, /factory/out/note/note.txt]\n"
    )


def _fail_runtime(world: _World, name: str) -> list[str]:
    try:
        qualify_runtime(world.ctx, world.key(name), profile="offline")
    except FactoryError as exc:
        return list(exc.detail.get("checks") or [])
    raise AssertionError(name + " unexpectedly passed")


def _check(receipt: dict, name: str) -> bool:
    return any(item["id"] == name and item["passed"] for item in receipt["checks"])
