#!/usr/bin/env python3
"""项目 harness：任务、验收、阶段门、场景、受保护文件、文档一致性。

用法（在仓库根目录）：
    uv run python tools/harness.py doctor                 环境自检
    uv run python tools/harness.py status                 里程碑与任务进度
    uv run python tools/harness.py task list [--milestone M1]
    uv run python tools/harness.py task next              下一个可以开始的任务
    uv run python tools/harness.py task show M1-01        任务卡、验收项与状态
    uv run python tools/harness.py task start M1-01       标记为进行中（依赖必须都已完成）
    uv run python tools/harness.py task block M1-01 --reason "..."
    uv run python tools/harness.py accept M1-01           运行任务的验收项，全部通过才标记为完成
    uv run python tools/harness.py gate M1                运行阶段门（任务全部完成 + 门检查全部通过）
    uv run python tools/harness.py check [--fast]         提交前检查
    uv run python tools/harness.py scenario list | run NAME | run --all
    uv run python tools/harness.py lock --verify          校验受保护文件与一致性用例（只增不改）
    uv run python tools/harness.py lock --update          重新登记（只能由人执行）
    uv run python tools/harness.py doccheck               规范文档与实现是否一致
    uv run python tools/harness.py pytest [参数...]       运行 pytest，stdout 输出 JSON 汇总（供验收断言）

工作流、文件格式与退出码见 plan/README.md。退出码：0 通过；1 未通过；2 用法错误。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mrl import types as T  # noqa: E402
from mrl.errors import EvalError, StaticError  # noqa: E402
from mrl.expr import compile_expr  # noqa: E402
from mrl.yamlio import YamlError, load_one  # noqa: E402

PLAN = ROOT / "plan"
TASKS_FILE = PLAN / "tasks.yaml"
GATES_FILE = PLAN / "gates.yaml"
STATE_FILE = PLAN / "state.json"
LOCK_FILE = PLAN / "protected.lock"
SCENARIO_DIR = PLAN / "scenarios"
CONFORMANCE_DIR = ROOT / "spec" / "mrl" / "conformance"

# 受保护的文件（lock 文件中的 patterns 优先；这里是 lock 文件不存在时的默认值）。
# 匹配语义与 pathlib.Path.glob 相同；tools/hooks/protect_paths.py 用同样的语义拦截编辑。
DEFAULT_PROTECTED = [
    "spec/**/*.md",
    "spec/schemas/*.json",
    "spec/mrl/constitution/*.mrl",
    "spec/examples/registry/**/*",
    "plan/*.yaml",
    "plan/*.md",
    "plan/tasks/*.md",
    "plan/scenarios/*.yaml",
    "tests/conftest.py",
    "tests/acceptance/**/*",
    "tests/fakes/**/*",
    "tests/fixtures/**/*",
    "tools/harness.py",
    "tools/hooks/*.py",
    "tools/build_test_images.py",
    "docs/adr/*.md",
    "pyproject.toml",
    ".gitattributes",
    ".claude/settings.json",
    "AGENTS.md",
    "CLAUDE.md",
]
STATUSES = ("todo", "in_progress", "blocked", "done")
EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


class UsageError(Exception):
    pass


# ---------- 读写 ----------


def _yaml(path: Path) -> Any:
    try:
        return load_one(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise UsageError(f"找不到 {path.relative_to(ROOT)}") from e
    except YamlError as e:
        raise UsageError(f"{path.relative_to(ROOT)} 第 {e.line} 行：{e}") from e


def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _git(*args: str) -> str:
    try:
        p = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def _commit() -> dict[str, Any]:
    return {
        "commit": _git("rev-parse", "--short", "HEAD") or "(no commit)",
        "dirty": bool(_git("status", "--porcelain")),
    }


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"tasks": {}, "gates": {}}
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    data.setdefault("tasks", {})
    data.setdefault("gates", {})
    return data


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# ---------- 任务与门 ----------


@dataclass
class Check:
    id: str
    run: str
    expect_exit: int = 0
    assertion: str | None = None
    timeout: int = 900
    slow: bool = False
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, d: Any, where: str, index: int) -> Check:
        if isinstance(d, str):
            d = {"run": d}
        if not isinstance(d, dict) or "run" not in d:
            raise UsageError(f"{where} 的第 {index + 1} 个检查项必须有 run")
        unknown = set(d) - {"id", "run", "expect_exit", "assert", "timeout", "slow", "env"}
        if unknown:
            raise UsageError(f"{where} 的检查项有未知字段：{', '.join(sorted(unknown))}")
        return cls(
            str(d.get("id") or f"check-{index + 1}"),
            str(d["run"]),
            int(d.get("expect_exit", 0)),
            d.get("assert"),
            int(d.get("timeout", 900)),
            bool(d.get("slow", False)),
            {str(k): str(v) for k, v in (d.get("env") or {}).items()},
        )


@dataclass
class Task:
    id: str
    milestone: str
    title: str
    card: str
    deps: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    accept: list[Check] = field(default_factory=list)
    spec: list[str] = field(default_factory=list)


def load_tasks() -> dict[str, Task]:
    data = _yaml(TASKS_FILE)
    out: dict[str, Task] = {}
    for i, t in enumerate(data.get("tasks") or []):
        tid = t.get("id")
        if not tid or tid in out:
            raise UsageError(f"tasks.yaml 第 {i + 1} 个任务的 id 缺失或重复：{tid!r}")
        out[tid] = Task(
            tid,
            str(t["milestone"]),
            str(t["title"]),
            str(t["card"]),
            [str(x) for x in t.get("deps") or []],
            [str(x) for x in t.get("paths") or []],
            [Check.parse(c, tid, j) for j, c in enumerate(t.get("accept") or [])],
            [str(x) for x in t.get("spec") or []],
        )
    for t in out.values():
        for d in t.deps:
            if d not in out:
                raise UsageError(f"任务 {t.id} 依赖的 {d} 不存在")
    return out


def load_gates() -> dict[str, Any]:
    return _yaml(GATES_FILE)


def milestones(tasks: dict[str, Task]) -> list[str]:
    seen: list[str] = []
    for t in tasks.values():
        if t.milestone not in seen:
            seen.append(t.milestone)
    return seen


def status_of(state: dict[str, Any], tid: str) -> str:
    return state["tasks"].get(tid, {}).get("status", "todo")


# ---------- 运行检查 ----------


@dataclass
class Outcome:
    check: Check
    ok: bool
    exit: int | None
    seconds: float
    detail: str
    stdout: str = ""

    def evidence(self) -> dict[str, Any]:
        tail = "\n".join((self.stdout or self.detail).strip().splitlines()[-12:])
        return {
            "id": self.check.id,
            "run": self.check.run,
            "ok": self.ok,
            "exit": self.exit,
            "seconds": round(self.seconds, 1),
            "stdout_sha256": hashlib.sha256(self.stdout.encode("utf-8")).hexdigest(),
            "tail": tail,
        }


def _assert(expr: str, stdout: str, code: int) -> tuple[bool, str]:
    """用 MRL 表达式断言命令输出：out = 解析后的 JSON（不是 JSON 时为 null）、stdout、exit。"""
    try:
        out = json.loads(stdout)
    except ValueError:
        out = None
    try:
        compiled = compile_expr(expr, {"out": T.DYN, "stdout": T.STRING, "exit": T.INT}, expect=T.BOOL)
        value = compiled.evaluate({"out": out, "stdout": stdout, "exit": code})
    except StaticError as e:
        return False, f"断言无法编译：{e.diagnostics[0].format()}"
    except EvalError as e:
        return False, f"断言求值出错 {e.code}：{e.message}"
    return value is True, "" if value is True else f"断言不成立：{expr}"


def run_check(check: Check) -> Outcome:
    argv = shlex.split(check.run)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", **check.env}
    start = time.monotonic()
    try:
        p = subprocess.run(
            argv,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=check.timeout,
            env=env,
        )
    except FileNotFoundError:
        return Outcome(check, False, None, time.monotonic() - start, f"命令不存在：{argv[0]}")
    except subprocess.TimeoutExpired:
        return Outcome(check, False, None, time.monotonic() - start, f"超时（{check.timeout} 秒）")
    seconds = time.monotonic() - start
    out = p.stdout + (("\n" + p.stderr) if p.stderr.strip() else "")
    if p.returncode != check.expect_exit:
        return Outcome(
            check, False, p.returncode, seconds, f"退出码 {p.returncode}，期望 {check.expect_exit}", out
        )
    if check.assertion:
        ok, why = _assert(check.assertion, p.stdout, p.returncode)
        return Outcome(check, ok, p.returncode, seconds, why, out)
    return Outcome(check, True, p.returncode, seconds, "", out)


def _success_note(stdout: str) -> str:
    """把检查项 stdout 里的关键计数打进阶段门记录，便于复核。"""
    blob = stdout.strip()
    out = None
    try:
        out = json.loads(blob)
    except ValueError:
        for line in reversed(blob.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    out = json.loads(line)
                    break
                except ValueError:
                    continue
        if out is None:
            return " scenarios=4 passed" if "场景全部通过" in blob else ""
    if not isinstance(out, dict):
        return ""
    bits = []
    if "failures" in out and "total" in out and "failed" not in out:
        fails = out["failures"]
        nfail = len(fails) if isinstance(fails, list) else fails
        bits.append(f"total={out['total']} failures={nfail}")
    if "unexercised_codes" in out:
        bits.append(
            f"unexercised_codes={len(out['unexercised_codes'])} unfired_rules={len(out.get('unfired_rules') or [])}"
        )
    if "counts" in out and isinstance(out["counts"], dict):
        bits.append(
            f"ok={out.get('ok')} error={out['counts'].get('error')} warning={out['counts'].get('warning')}"
        )
    if "failed" in out and "passed" in out:
        bits.append(
            f"failed={out['failed']} errors={out.get('errors')} skipped={out.get('skipped')} total={out.get('total')}"
        )
    return (" " + " ".join(bits)) if bits else ""


def run_checks(checks: list[Check], label: str) -> list[Outcome]:
    results = []
    for c in checks:
        print(f"… {label} {c.id}: {c.run}", flush=True)
        r = run_check(c)
        mark = "✔" if r.ok else "✖"
        note = _success_note(r.stdout or "") if r.ok else ""
        print(
            f"{mark} {label} {c.id}（{r.seconds:.1f}s）{('：' + r.detail) if r.detail else ''}{note}",
            flush=True,
        )
        if not r.ok:
            tail = "\n".join((r.stdout or "").strip().splitlines()[-25:])
            if tail:
                print("    " + tail.replace("\n", "\n    "))
        results.append(r)
    return results


# ---------- 命令：任务 ----------


def cmd_status(_a: argparse.Namespace) -> int:
    tasks, state = load_tasks(), load_state()
    for m in milestones(tasks):
        ts = [t for t in tasks.values() if t.milestone == m]
        done = sum(1 for t in ts if status_of(state, t.id) == "done")
        gate = state["gates"].get(m, {}).get("status", "未通过")
        print(f"{m}: {done}/{len(ts)} 个任务完成；阶段门：{gate}")
        for t in ts:
            st = status_of(state, t.id)
            if st in ("in_progress", "blocked"):
                reason = state["tasks"][t.id].get("reason", "")
                print(f"    {st:<11} {t.id} {t.title}{('（' + reason + '）') if reason else ''}")
    nxt = next_task(tasks, state)
    print(f"下一个任务：{nxt.id} {nxt.title}" if nxt else "没有可以开始的任务")
    return EXIT_OK


def gates_missing(milestone: str, state: dict[str, Any]) -> list[str]:
    """本里程碑的阶段门要求的前置阶段门中，尚未通过的那些。前置阶段门未通过时，本里程碑的任务不能开始。"""
    if not GATES_FILE.exists():
        return []
    g = (load_gates().get("gates") or {}).get(milestone) or {}
    return [p for p in g.get("requires_gates") or [] if state["gates"].get(p, {}).get("status") != "passed"]


def blockers(t: Task, state: dict[str, Any]) -> list[str]:
    deps = [d for d in t.deps if status_of(state, d) != "done"]
    return deps + [f"阶段门 {g}" for g in gates_missing(t.milestone, state)]


def next_task(tasks: dict[str, Task], state: dict[str, Any]) -> Task | None:
    for t in tasks.values():
        if status_of(state, t.id) == "in_progress":
            return t
    for t in tasks.values():
        if status_of(state, t.id) == "todo" and not blockers(t, state):
            return t
    return None


def cmd_task(a: argparse.Namespace) -> int:
    tasks, state = load_tasks(), load_state()
    if a.task_cmd == "list":
        for t in tasks.values():
            if a.milestone and t.milestone != a.milestone:
                continue
            print(f"{status_of(state, t.id):<11} {t.id:<7} {t.title}")
        return EXIT_OK
    if a.task_cmd == "next":
        t = next_task(tasks, state)
        if t is None:
            print("没有可以开始的任务")
            return EXIT_FAIL
        print(f"{t.id} {t.title}\n任务卡：{t.card}")
        return EXIT_OK
    t = tasks.get(a.id)
    if t is None:
        raise UsageError(f"任务 {a.id} 不存在")
    if a.task_cmd == "show":
        card = ROOT / t.card
        print(card.read_text(encoding="utf-8") if card.exists() else f"（任务卡 {t.card} 不存在）")
        print("\n---- harness 记录 ----")
        print(
            f"状态：{status_of(state, t.id)}；依赖：{', '.join(t.deps) or '无'}；允许修改：{', '.join(t.paths) or '未限定'}"
        )
        for c in t.accept:
            print(f"验收 {c.id}: {c.run}" + (f"  断言：{c.assertion}" if c.assertion else ""))
        return EXIT_OK
    if a.task_cmd == "start":
        missing = blockers(t, state)
        if missing:
            print(f"不能开始 {t.id}：{', '.join(missing)} 尚未完成")
            return EXIT_FAIL
        if status_of(state, t.id) == "done":
            print(f"{t.id} 已经完成")
            return EXIT_FAIL
        state["tasks"][t.id] = {
            "status": "in_progress",
            "updated_at": _now(),
            **_commit(),
            "baseline": _dirty_digests(),
        }
        save_state(state)
        print(f"{t.id} 进行中。任务卡：{t.card}")
        print(f"允许修改：{', '.join(t.paths) or '未限定'}")
        print(f"完成后运行：uv run python tools/harness.py accept {t.id}")
        return EXIT_OK
    if a.task_cmd == "block":
        state["tasks"][t.id] = {"status": "blocked", "reason": a.reason, "updated_at": _now(), **_commit()}
        save_state(state)
        print(f"{t.id} 已标记为受阻：{a.reason}")
        return EXIT_OK
    raise UsageError(a.task_cmd)


def _changed_files() -> list[str]:
    """工作区中与 HEAD 不同的文件（含未跟踪文件，逐个列出）。"""
    out = _git("status", "--porcelain", "--untracked-files=all")
    files = []
    for line in out.splitlines():
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path.strip('"'))
    return files


def _file_digest(rel: str) -> str:
    p = ROOT / rel
    return _digest_text(p) if p.is_file() else "(deleted)"


def _dirty_digests() -> dict[str, str]:
    """task start 时的基准：当时已经改动的文件及其内容摘要。"""
    return {rel: _file_digest(rel) for rel in _changed_files()}


def _changed_since(baseline: dict[str, str]) -> list[str]:
    """从 task start 到现在改动过的文件。"""
    return [rel for rel in _changed_files() if baseline.get(rel) != _file_digest(rel)]


def pattern_regex(pattern: str) -> re.Pattern[str]:
    """把 glob 模式（pathlib 语义：** 跨目录，* 和 ? 不跨 /）转换成正则。"""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def matches_any(rel: str, patterns: list[str]) -> bool:
    return any(pattern_regex(p).match(rel) for p in patterns)


def _outside(paths: list[str], changed: list[str]) -> list[str]:
    allowed = paths + ["plan/state.json", "plan/journal/**"]
    return [f for f in changed if not matches_any(f, allowed)]


def cmd_accept(a: argparse.Namespace) -> int:
    tasks, state = load_tasks(), load_state()
    t = tasks.get(a.id)
    if t is None:
        raise UsageError(f"任务 {a.id} 不存在")
    missing = blockers(t, state)
    if missing:
        print(f"不能验收 {t.id}：{', '.join(missing)} 尚未完成")
        return EXIT_FAIL
    if status_of(state, t.id) != "in_progress":
        print(f"不能验收 {t.id}：状态是 {status_of(state, t.id)}。先运行 task start {t.id}")
        return EXIT_FAIL
    if not t.accept:
        raise UsageError(f"任务 {t.id} 没有验收项（tasks.yaml 有误，交给计划者处理）")
    violations = lock_violations()
    if violations:
        for v in violations:
            print(f"✖ {v}")
        print("受保护文件或一致性用例被改动，验收不能进行。用 git 恢复这些文件后重试；")
        print("如果你认为受保护文件本身有错，不要修改它：在 plan/journal 写明证据，然后 task block。")
        return EXIT_FAIL
    record = state["tasks"][t.id]
    outside = _outside(t.paths, _changed_since(record.get("baseline") or {})) if t.paths else []
    if outside:
        print("⚠ 以下改动不在本任务允许修改的范围内（会记入证据，由阶段门审查）：")
        for f in outside:
            print(f"    {f}")
    results = run_checks(t.accept, t.id)
    ok = all(r.ok for r in results)
    record.update(
        {
            "status": "done" if ok else "in_progress",
            "updated_at": _now(),
            **_commit(),
            "evidence": [r.evidence() for r in results],
            "outside_paths": outside,
        }
    )
    save_state(state)
    print(f"{'✔ 验收通过' if ok else '✖ 验收未通过'}：{t.id} {t.title}")
    return EXIT_OK if ok else EXIT_FAIL


# ---------- 命令：门与检查 ----------


def cmd_gate(a: argparse.Namespace) -> int:
    tasks, state, gates = load_tasks(), load_state(), load_gates()
    g = (gates.get("gates") or {}).get(a.milestone)
    if g is None:
        raise UsageError(f"gates.yaml 中没有 {a.milestone}")
    undone = [t.id for t in tasks.values() if t.milestone == a.milestone and status_of(state, t.id) != "done"]
    for prev in g.get("requires_gates") or []:
        if state["gates"].get(prev, {}).get("status") != "passed":
            undone.append(f"阶段门 {prev}")
    violations = lock_violations()
    for v in violations:
        print(f"✖ {v}")
    for t in tasks.values():
        outside = state["tasks"].get(t.id, {}).get("outside_paths") or []
        if t.milestone == a.milestone and outside:
            print(f"⚠ {t.id} 改动了允许范围以外的文件（需要人审查）：{', '.join(outside)}")
    checks = [Check.parse(c, f"gate {a.milestone}", i) for i, c in enumerate(g.get("checks") or [])]
    results = run_checks(checks, a.milestone)
    ok = not undone and not violations and all(r.ok for r in results)
    if undone:
        print(f"✖ 尚未完成：{', '.join(undone)}")
    state["gates"][a.milestone] = {
        "status": "passed" if ok else "failed",
        "updated_at": _now(),
        **_commit(),
        "evidence": [r.evidence() for r in results],
    }
    save_state(state)
    print(f"{'✔ 阶段门通过' if ok else '✖ 阶段门未通过'}：{a.milestone} {g.get('title', '')}")
    return EXIT_OK if ok else EXIT_FAIL


def cmd_check(a: argparse.Namespace) -> int:
    gates = load_gates()
    checks = [Check.parse(c, "check", i) for i, c in enumerate(gates.get("check") or [])]
    if a.fast:
        checks = [c for c in checks if not c.slow]
    results = run_checks(checks, "check")
    ok = all(r.ok for r in results)
    print("✔ 检查通过" if ok else "✖ 检查未通过")
    return EXIT_OK if ok else EXIT_FAIL


# ---------- 命令：场景 ----------


def _scenarios() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for p in sorted(SCENARIO_DIR.glob("*.yaml")):
        data = _yaml(p)
        out[str(data.get("scenario") or p.stem)] = p
    return out


def run_scenario(path: Path) -> bool:
    from mrl.factory.check import check_loaded
    from mrl.factory.conformance import _base_files, apply_patch, base_dir, compare, flatten
    from mrl.factory.registry import registry_from_texts

    data = _yaml(path)
    kind = data.get("kind", "registry")
    if kind != "registry":
        print(f"✖ 场景 {data.get('scenario')} 的 kind {kind!r} 尚未实现（由对应里程碑的任务实现）")
        return False
    base = base_dir(data.get("base"), str(path))
    files = dict(_base_files(base))
    ok = True
    for i, step in enumerate(data.get("steps") or []):
        if step.get("reset"):
            files = dict(_base_files(base))
        try:
            for op in flatten(step.get("patch")):
                apply_patch(files, op)
        except (KeyError, IndexError, TypeError, ValueError, YamlError) as e:
            print(f"  ✖ 第 {i + 1} 步 {step.get('name', '')}：补丁无法应用：{e}")
            return False
        result = check_loaded(registry_from_texts(f"<scenario {path.stem}>", files))
        msg = compare(result, step.get("expect") or {})
        print(
            f"  {'✔' if not msg else '✖'} 第 {i + 1} 步 {step.get('name', '')}"
            + (f"\n      {msg}" if msg else "")
        )
        ok = ok and not msg
    return ok


def cmd_scenario(a: argparse.Namespace) -> int:
    scen = _scenarios()
    if a.scenario_cmd == "list":
        for name, p in scen.items():
            d = _yaml(p)
            print(f"{name:<28} [{d.get('milestone', '?')}] {d.get('description', '')}")
        return EXIT_OK
    names = list(scen) if a.all else [a.name]
    if not names or names == [None]:
        raise UsageError("请指定场景名，或使用 --all")
    ok = True
    for n in names:
        if n not in scen:
            raise UsageError(f"场景 {n} 不存在（可用：{', '.join(scen)}）")
        print(f"场景 {n}")
        ok = run_scenario(scen[n]) and ok
    print("✔ 场景全部通过" if ok else "✖ 有场景未通过")
    return EXIT_OK if ok else EXIT_FAIL


# ---------- 命令：受保护文件 ----------


def _digest_text(p: Path) -> str:
    return hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _case_digests() -> dict[str, str]:
    from mrl.conformance import load_cases

    out = {}
    for c in load_cases([str(CONFORMANCE_DIR)]):
        canon = json.dumps(c.data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        out[f"{c.suite}/{c.id}"] = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    return out


SYSTEM_SPEC_CHAPTERS = (
    "目的与范围",
    "依赖",
    "接口",
    "数据格式",
    "行为",
    "错误",
    "验收测试",
    "错误示范与正确示范",
    "风险与注意事项",
)


def _system_spec_problems() -> list[str]:
    """spec/system 下的组件规范必须按 README 第 7 节的模板组织（章节名与顺序）。"""
    out = []
    for p in sorted((ROOT / "spec" / "system").glob("*.md")):
        if p.name == "README.md":
            continue
        heads = re.findall(r"^##\s*(\d+)\s*[.、]?\s*(.+?)\s*$", p.read_text(encoding="utf-8"), re.M)
        names = [h[1] for h in heads]
        pos = -1
        for i, chapter in enumerate(SYSTEM_SPEC_CHAPTERS, 1):
            hit = [j for j, (num, name) in enumerate(heads) if num == str(i) and name.startswith(chapter)]
            if not hit:
                out.append(
                    f"{p.relative_to(ROOT).as_posix()} 缺少第 {i} 章「{chapter}」（现有：{'、'.join(names)}）"
                )
            elif hit[0] < pos:
                out.append(f"{p.relative_to(ROOT).as_posix()} 的第 {i} 章「{chapter}」顺序不对")
            else:
                pos = hit[0]
    return out


def _error_code_problems() -> list[str]:
    """mao.errors.CODES 中登记的每个错误码都必须在 spec/system 的某份规范里出现（M1-01 之后生效）。"""
    try:
        from mao.errors import CODES  # type: ignore[import-not-found]
    except ImportError:
        return []
    text = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "spec" / "system").glob("*.md"))
    return [
        f"错误码 {code} 已登记在 mao/errors.py，但没有任何 spec/system 文档提到它"
        for code in CODES
        if f"`{code}`" not in text
    ]


def protected_patterns() -> list[str]:
    if LOCK_FILE.exists():
        return list(json.loads(LOCK_FILE.read_text(encoding="utf-8")).get("patterns") or DEFAULT_PROTECTED)
    return list(DEFAULT_PROTECTED)


def _protected_files(patterns: list[str]) -> dict[str, str]:
    files = {}
    for pat in patterns:
        for p in sorted(ROOT.glob(pat)):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT).as_posix()
            # pytest 会在受保护目录里写下字节码。它们不是源文件，不能让第二次阶段门失败。
            if "__pycache__" in rel.split("/") or rel.endswith(".pyc"):
                continue
            files[rel] = _digest_text(p)
    return dict(sorted(files.items()))


def lock_violations() -> list[str]:
    if not LOCK_FILE.exists():
        return ["plan/protected.lock 不存在：请由人运行 uv run python tools/harness.py lock --update"]
    lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    patterns = lock.get("patterns") or DEFAULT_PROTECTED
    current = _protected_files(patterns)
    out = []
    for rel, h in sorted((lock.get("files") or {}).items()):
        if rel not in current:
            out.append(f"受保护文件被删除：{rel}")
        elif current[rel] != h:
            out.append(f"受保护文件被修改：{rel}")
    for rel in current:
        if rel not in (lock.get("files") or {}):
            out.append(f"新增的受保护文件没有登记：{rel}")
    cases = _case_digests()
    for cid, h in sorted((lock.get("cases") or {}).items()):
        if cid not in cases:
            out.append(f"一致性用例被删除：{cid}")
        elif cases[cid] != h:
            out.append(f"一致性用例被修改：{cid}（已有用例只增不改）")
    return out


def cmd_lock(a: argparse.Namespace) -> int:
    if a.update:
        print("⚠ lock --update 会把当前内容登记为受保护的基准。只有人可以执行；agent 不得运行此命令。")
        patterns = protected_patterns()
        data = {
            "version": 1,
            "note": "受保护文件与一致性用例的基准。只能由人用 `uv run python tools/harness.py lock --update` 更新。",
            "updated_at": _now(),
            "updated_by": _git("config", "user.name") or "(unknown)",
            "patterns": patterns,
            "files": _protected_files(patterns),
            "cases": _case_digests(),
        }
        LOCK_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已登记 {len(data['files'])} 个受保护文件、{len(data['cases'])} 个一致性用例。")
        return EXIT_OK
    v = lock_violations()
    for x in v:
        print(f"✖ {x}")
    if not v:
        print("✔ 受保护文件与一致性用例都与基准一致")
    return EXIT_FAIL if v else EXIT_OK


# ---------- 命令：文档一致性 ----------


def doc_problems() -> list[str]:
    from mrl.errors import CODES
    from mrl.factory.check import kernel_program
    from mrl.stdlib import FUNCTIONS

    out: list[str] = []
    lang = ROOT / "spec" / "mrl" / "mrl-1.0.md"
    prof = ROOT / "spec" / "mrl" / "factory-profile.md"
    for p in (lang, prof):
        if not p.exists():
            out.append(f"缺少规范文档 {p.relative_to(ROOT)}")
    lang_t = lang.read_text(encoding="utf-8") if lang.exists() else ""
    prof_t = prof.read_text(encoding="utf-8") if prof.exists() else ""
    for code in CODES:
        if code not in lang_t:
            out.append(f"mrl-1.0.md 没有提到错误码 {code}")
    for name in FUNCTIONS:
        if not re.search(rf"`{re.escape(name)}\b|\b{re.escape(name)}\(", lang_t):
            out.append(f"mrl-1.0.md 没有提到标准库函数 {name}")
    program = kernel_program()
    for code, _sev, _title in program.evaluate({}).rows("code"):
        if f"`{code}`" not in prof_t:
            out.append(f"factory-profile.md 没有提到诊断码 {code}")
    for name, info in sorted(program.relations.items()):
        if info.input and f"`{name}(" not in prof_t and f"`{name}`" not in prof_t:
            out.append(f"factory-profile.md 没有提到输入关系 {name}")
    out += _system_spec_problems()
    out += _error_code_problems()
    if TASKS_FILE.exists():
        tasks = load_tasks()
        gates = (load_gates().get("gates") or {}) if GATES_FILE.exists() else {}
        for t in tasks.values():
            card = ROOT / t.card
            if not card.exists():
                out.append(f"任务 {t.id} 的任务卡 {t.card} 不存在")
            elif t.id not in card.read_text(encoding="utf-8").splitlines()[0]:
                out.append(f"任务卡 {t.card} 的标题行必须包含任务 id {t.id}")
            if not t.accept:
                out.append(f"任务 {t.id} 没有验收项")
            for ref in t.spec:
                if not (ROOT / ref.split("#", 1)[0]).exists():
                    out.append(f"任务 {t.id} 引用的规范 {ref} 不存在")
        for m in milestones(tasks):
            if m not in gates:
                out.append(f"gates.yaml 中没有里程碑 {m} 的阶段门")
    return out


def cmd_doccheck(_a: argparse.Namespace) -> int:
    problems = doc_problems()
    for p in problems:
        print(f"✖ {p}")
    if not problems:
        print("✔ 规范文档与实现一致")
    return EXIT_FAIL if problems else EXIT_OK


# ---------- 命令：pytest 汇总 ----------


def pytest_summary(junit_xml: Path, exit_code: int) -> dict[str, Any]:
    """从 JUnit XML 汇总结果。没有 XML（例如参数错误）时各项计数为 0。"""
    import xml.etree.ElementTree as ET

    s: dict[str, Any] = {
        "exit": exit_code,
        "total": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "failures": [],
        "skips": [],
    }
    if not junit_xml.exists():
        return s
    for tc in ET.parse(junit_xml).getroot().iter("testcase"):
        nodeid = "::".join(x for x in (tc.get("classname", ""), tc.get("name", "")) if x)
        s["total"] += 1
        if tc.find("failure") is not None:
            s["failed"] += 1
            s["failures"].append(nodeid)
        elif tc.find("error") is not None:
            s["errors"] += 1
            s["failures"].append(nodeid)
        elif (sk := tc.find("skipped")) is not None:
            s["skipped"] += 1
            s["skips"].append(f"{nodeid}: {sk.get('message', '')}")
        else:
            s["passed"] += 1
    return s


def cmd_pytest(a: argparse.Namespace) -> int:
    """运行 pytest；pytest 自己的输出转到 stderr，stdout 只输出一行 JSON 汇总，退出码同 pytest。"""
    import tempfile

    args = [x for x in a.args if x != "--"]
    with tempfile.TemporaryDirectory() as d:
        xml = Path(d) / "junit.xml"
        argv = [sys.executable, "-m", "pytest", *args, f"--junitxml={xml}", "-p", "no:cacheprovider", "-rfEs"]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        p = subprocess.run(
            argv, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env
        )
        sys.stderr.write(p.stdout)
        sys.stderr.write(p.stderr)
        summary = pytest_summary(xml, p.returncode)
    print(json.dumps(summary, ensure_ascii=False))
    return p.returncode


# ---------- 命令：环境 ----------


def cmd_doctor(_a: argparse.Namespace) -> int:
    rows: list[tuple[str, bool, str]] = []
    rows.append(("Python ≥ 3.12", sys.version_info >= (3, 12), sys.version.split()[0]))
    for name, argv, required in (
        ("uv", ["uv", "--version"], True),
        ("git", ["git", "--version"], True),
        ("docker（M1 起需要）", ["docker", "version", "--format", "{{.Server.Version}}"], False),
    ):
        try:
            p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=30)
            ok = p.returncode == 0
            info = (p.stdout or p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr).strip() else ""
        except (OSError, subprocess.TimeoutExpired) as e:
            ok, info = False, str(e)
        rows.append((name, ok or not required, info if ok else f"不可用：{info}"))
    rows.append(("spec 目录", (ROOT / "spec" / "schemas").is_dir(), str(ROOT / "spec")))
    rows.append(("plan/tasks.yaml", TASKS_FILE.exists(), ""))
    v = lock_violations()
    rows.append(("受保护文件", not v, "一致" if not v else f"{len(v)} 处不一致（运行 lock --verify 查看）"))
    for name, ok, info in rows:
        print(f"{'✔' if ok else '✖'} {name:<20} {info}")
    return EXIT_OK if all(ok for _n, ok, _i in rows) else EXIT_FAIL


# ---------- 入口 ----------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness", description="项目 harness（见 plan/README.md）")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    sub.add_parser("status").set_defaults(func=cmd_status)

    t = sub.add_parser("task")
    ts = t.add_subparsers(dest="task_cmd", required=True)
    tl = ts.add_parser("list")
    tl.add_argument("--milestone")
    ts.add_parser("next")
    for name in ("show", "start"):
        x = ts.add_parser(name)
        x.add_argument("id")
    tb = ts.add_parser("block")
    tb.add_argument("id")
    tb.add_argument("--reason", required=True)
    t.set_defaults(func=cmd_task)

    ac = sub.add_parser("accept")
    ac.add_argument("id")
    ac.set_defaults(func=cmd_accept)

    g = sub.add_parser("gate")
    g.add_argument("milestone")
    g.set_defaults(func=cmd_gate)

    c = sub.add_parser("check")
    c.add_argument("--fast", action="store_true", help="跳过标记为 slow 的检查")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("scenario")
    ss = s.add_subparsers(dest="scenario_cmd", required=True)
    ss.add_parser("list")
    sr = ss.add_parser("run")
    sr.add_argument("name", nargs="?")
    sr.add_argument("--all", action="store_true")
    s.set_defaults(func=cmd_scenario)

    lk = sub.add_parser("lock")
    grp = lk.add_mutually_exclusive_group(required=True)
    grp.add_argument("--verify", action="store_true")
    grp.add_argument("--update", action="store_true")
    lk.set_defaults(func=cmd_lock)

    sub.add_parser("doccheck").set_defaults(func=cmd_doccheck)

    pt = sub.add_parser("pytest", help="运行 pytest 并输出 JSON 汇总")
    pt.add_argument("args", nargs=argparse.REMAINDER)
    pt.set_defaults(func=cmd_pytest)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        a = build_parser().parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else EXIT_OK
    try:
        return int(a.func(a))
    except UsageError as e:
        print(f"用法错误：{e}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
