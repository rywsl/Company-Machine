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
    uv run python tools/harness.py accept M1-01 --only check-id  开发诊断单项检查（不完成任务）
    uv run python tools/harness.py accept M1-01 --resume       复用未变化的离线成功项，live 仍重跑
    uv run python tools/harness.py gate M1                运行阶段门（任务全部完成 + 门检查全部通过）
    uv run python tools/harness.py review audit M1-01      只读分析越界改动与 reviewer 状态
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
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
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
FACTORY_DIR = ROOT / ".factory"
HARNESS_RUNS_DIR = FACTORY_DIR / "harness-runs"
HARNESS_CACHE_DIR = FACTORY_DIR / "harness-cache"
HARNESS_REVIEWS_DIR = FACTORY_DIR / "reviews"

# 受保护的文件（lock 文件中的 patterns 优先；这里是 lock 文件不存在时的默认值）。
# 匹配语义与 pathlib.Path.glob 相同；tools/hooks/protect_paths.py 用同样的语义拦截编辑。
DEFAULT_PROTECTED = [
    "spec/**/*.md",
    "spec/schemas/*.json",
    "spec/mrl/constitution/*.mrl",
    "spec/mrl/governance/*.mrl",
    "spec/examples/registry/**/*",
    "plan/*.yaml",
    "plan/*.md",
    "plan/tasks/*.md",
    "plan/scenarios/*.yaml",
    "tests/conftest.py",
    "tests/test_hooks.py",
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
    "docs/*.md",
    "tests/test_codebase_policy.py",
    "tests/**/conftest.py",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "spec/runtimes/*.yaml",
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


def _git_raw(*args: str) -> str:
    try:
        p = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.rstrip("\r\n") if p.returncode == 0 else ""


def _git(*args: str) -> str:
    return _git_raw(*args).strip()


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


def _atomic_write_text(path: Path, text: str, retries: int = 5) -> None:
    """用短暂退避完成 Windows 文件替换，避免 WinError 5 迫使整轮验收重跑。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    last: OSError | None = None
    for attempt in range(retries):
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
            return
        except OSError as exc:
            last = exc
            if not (os.name == "nt" and getattr(exc, "winerror", None) in (5, 32)):
                raise
            time.sleep(0.05 * (2**attempt))
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # 下一次替换仍可能成功；最终失败时保留原始异常。
                pass
    if last is not None:
        raise last


def save_state(state: dict[str, Any]) -> None:
    _atomic_write_text(
        STATE_FILE,
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
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
    parallel_group: str | None = None
    exclusive_resources: tuple[str, ...] = ("workspace", "state")
    cacheable: bool = False
    cache_inputs: tuple[str, ...] = ()
    live: bool = False
    requires: tuple[str, ...] = ()

    @classmethod
    def parse(cls, d: Any, where: str, index: int) -> Check:
        if isinstance(d, str):
            d = {"run": d}
        if not isinstance(d, dict) or "run" not in d:
            raise UsageError(f"{where} 的第 {index + 1} 个检查项必须有 run")
        unknown = set(d) - {
            "id",
            "run",
            "expect_exit",
            "assert",
            "timeout",
            "slow",
            "env",
            "parallel_group",
            "exclusive_resources",
            "cacheable",
            "cache_inputs",
            "live",
            "requires",
        }
        if unknown:
            raise UsageError(f"{where} 的检查项有未知字段：{', '.join(sorted(unknown))}")
        live = bool(d.get("live", False))
        cacheable = bool(d.get("cacheable", False))
        if live and cacheable:
            raise UsageError(
                f"{where} 的检查项 {d.get('id') or index + 1} 不能同时 live=true 和 cacheable=true"
            )
        resources = d.get("exclusive_resources", ("workspace", "state"))
        if resources is None:
            resources = ()
        if isinstance(resources, str):
            resources = (resources,)
        cache_inputs = d.get("cache_inputs", ()) or ()
        requires = d.get("requires", ()) or ()
        if isinstance(cache_inputs, str):
            cache_inputs = (cache_inputs,)
        if isinstance(requires, str):
            requires = (requires,)
        return cls(
            str(d.get("id") or f"check-{index + 1}"),
            str(d["run"]),
            int(d.get("expect_exit", 0)),
            d.get("assert"),
            int(d.get("timeout", 900)),
            bool(d.get("slow", False)),
            {str(k): str(v) for k, v in (d.get("env") or {}).items()},
            str(d["parallel_group"]) if d.get("parallel_group") else None,
            tuple(str(x) for x in resources),
            cacheable,
            tuple(str(x) for x in cache_inputs),
            live,
            tuple(str(x) for x in requires),
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
    stderr: str = ""
    executed: bool = True
    reused: bool = False
    fingerprint: str = ""
    temp_dir: str = ""
    resources: tuple[str, ...] = ()
    cleanup: dict[str, Any] = field(default_factory=dict)
    blocker: str | None = None
    evidence_path: str = ""

    def evidence(self) -> dict[str, Any]:
        tail = "\n".join((self.stdout or self.detail).strip().splitlines()[-12:])
        return {
            "id": self.check.id,
            "run": self.check.run,
            "ok": self.ok,
            "exit": self.exit,
            "seconds": round(self.seconds, 1),
            "stdout_sha256": hashlib.sha256(self.stdout.encode("utf-8")).hexdigest(),
            "stderr_sha256": hashlib.sha256(self.stderr.encode("utf-8")).hexdigest(),
            "tail": tail,
            "executed": self.executed,
            "reused": self.reused,
            "fingerprint": self.fingerprint,
            "temp_dir": self.temp_dir,
            "exclusive_resources": list(self.resources),
            "cleanup": self.cleanup,
            "blocker": self.blocker,
            "evidence_path": self.evidence_path,
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


_LAST_RUN_MANIFEST: Path | None = None


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "check"


def _command_version(argv: list[str]) -> str:
    try:
        p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    return (p.stdout or p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr).strip() else "unknown"


def _environment_fingerprint(check: Check) -> dict[str, Any]:
    names = set(check.env)
    names.update({"PYTHONIOENCODING", "PYTHONUTF8", "MAO_HARNESS_TEMP_DIR"})
    names.update(item[4:] for item in check.cache_inputs if item.startswith("env:") and item[4:])
    values = {}
    for name in sorted(names):
        value = check.env.get(name, os.environ.get(name, ""))
        values[name] = {
            "set": bool(value),
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest() if value else "",
        }
    return values


def _input_files_for_fingerprint(check: Check, *, include_cache_inputs: bool = True) -> dict[str, str]:
    files: set[Path] = set()
    for base in (ROOT / "spec", ROOT / "plan", ROOT / "tests"):
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and (p.suffix.lower() in {".py", ".yaml", ".yml", ".md", ".json", ".toml"}):
                files.add(p)
    for item in check.cache_inputs if include_cache_inputs else ():
        if item.startswith("env:"):
            continue
        candidate = ROOT / item
        if any(ch in item for ch in "*?["):
            files.update(p for p in ROOT.glob(item) if p.is_file())
        elif candidate.is_file():
            files.add(candidate)
    result = {}
    for p in sorted(files):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT).as_posix()
        # state.json is a harness output. Including it would invalidate every
        # cache entry immediately after accept writes the latest run evidence.
        if rel == "plan/state.json":
            continue
        result[rel] = _digest_text(p)
    return result


def _fingerprint_base() -> dict[str, Any]:
    changed = [rel for rel in _changed_files() if rel != "plan/state.json"]
    return {
        "source": {
            "commit": _git("rev-parse", "HEAD"),
            "changed_digests": {rel: _file_digest(rel) for rel in changed},
        },
        "inputs": _input_files_for_fingerprint(Check(id="__base__", run=""), include_cache_inputs=False),
        "tools": {
            "python": sys.version,
            "uv": _command_version(["uv", "--version"]),
            "pytest": _command_version([sys.executable, "-m", "pytest", "--version"]),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
    }


def _cache_fingerprint(check: Check, base: dict[str, Any] | None = None) -> str:
    payload = {
        **(base or _fingerprint_base()),
        "environment": _environment_fingerprint(check),
        "cache_inputs": _input_files_for_fingerprint(check, include_cache_inputs=True),
        "check": {
            "id": check.id,
            "run": check.run,
            "expect_exit": check.expect_exit,
            "assert": check.assertion,
            "timeout": check.timeout,
            "cache_inputs": list(check.cache_inputs),
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _evidence_path(check: Check) -> str:
    candidates = []
    for token in shlex.split(check.run):
        p = ROOT / token
        if p.exists() and (p.is_file() or p.suffix in {".json", ".yaml", ".yml"}):
            candidates.append(p.relative_to(ROOT).as_posix())
    return ",".join(candidates[:4])


def _cache_allowed(check: Check) -> bool:
    if not check.cacheable or check.live:
        return False
    # 保守兜底：即使任务作者误标元数据，live/资格/模型/证据语义也不能进入缓存。
    forbidden = (
        "live",
        "docker",
        "model",
        "qualification",
        "runtime.command",
        "verify_live",
        "operability",
        "budget",
        "credential",
    )
    return not any(word in check.run.lower() for word in forbidden)


def _terminate_process_tree(pid: int | None) -> dict[str, Any]:
    if not pid:
        return {"attempted": False, "ok": True, "method": "none"}
    if os.name == "nt":
        try:
            p = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
            # 128 means the root exited between communicate() and cleanup; no live tree remains.
            return {
                "attempted": True,
                "ok": p.returncode in (0, 128),
                "method": "taskkill",
                "exit": p.returncode,
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"attempted": True, "ok": False, "method": "taskkill", "error": str(exc)}
    try:
        os.killpg(pid, signal.SIGTERM)
        return {"attempted": True, "ok": True, "method": "killpg"}
    except ProcessLookupError:
        return {"attempted": True, "ok": True, "method": "killpg", "already_exited": True}
    except OSError as exc:
        return {"attempted": True, "ok": False, "method": "killpg", "error": str(exc)}


def run_check(
    check: Check,
    *,
    temp_dir: Path | None = None,
    fingerprint: str = "",
) -> Outcome:
    argv = shlex.split(check.run)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", **check.env}
    if temp_dir is not None:
        temp_dir.mkdir(parents=True, exist_ok=True)
        env.update(
            {
                "MAO_HARNESS_TEMP_DIR": str(temp_dir),
                "MAO_HARNESS_CHECK_ID": check.id,
            }
        )
        # Pytest and Docker fixtures need a user-owned system temp root on
        # Windows. Keep harness evidence isolated while avoiding root-owned
        # bind-mounted .git objects that cannot be cleaned by the test user.
        if "tools/harness.py pytest" not in check.run:
            env.update(
                {
                    "TMPDIR": str(temp_dir),
                    "TEMP": str(temp_dir),
                    "TMP": str(temp_dir),
                }
            )
    start = time.monotonic()
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    kwargs: dict[str, Any] = {
        "cwd": ROOT,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": env,
        "creationflags": creationflags,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
    try:
        p = subprocess.Popen(argv, **kwargs)
    except FileNotFoundError:
        return Outcome(
            check,
            False,
            None,
            time.monotonic() - start,
            f"命令不存在：{argv[0] if argv else check.run}",
            fingerprint=fingerprint,
            temp_dir=str(temp_dir or ""),
            resources=check.exclusive_resources,
            evidence_path=_evidence_path(check),
        )
    except OSError as exc:
        return Outcome(
            check,
            False,
            None,
            time.monotonic() - start,
            f"进程启动失败：{exc}",
            fingerprint=fingerprint,
            temp_dir=str(temp_dir or ""),
            resources=check.exclusive_resources,
            blocker="process_start",
            evidence_path=_evidence_path(check),
        )
    cleanup = {"attempted": False, "ok": True, "pid": p.pid}
    try:
        try:
            stdout, stderr = p.communicate(timeout=check.timeout)
        except subprocess.TimeoutExpired:
            cleanup = _terminate_process_tree(p.pid)
            try:
                stdout, stderr = p.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                stdout, stderr = p.communicate()
            return Outcome(
                check,
                False,
                None,
                time.monotonic() - start,
                f"超时（{check.timeout} 秒）",
                stdout or "",
                stderr or "",
                fingerprint=fingerprint,
                temp_dir=str(temp_dir or ""),
                resources=check.exclusive_resources,
                cleanup=cleanup,
                blocker="timeout" if cleanup.get("ok") else "process_cleanup",
                evidence_path=_evidence_path(check),
            )
    finally:
        if p.poll() is None:
            cleanup = _terminate_process_tree(p.pid)
    seconds = time.monotonic() - start
    stdout, stderr = stdout or "", stderr or ""
    combined = stdout + (("\n" + stderr) if stderr.strip() else "")
    common = {
        "fingerprint": fingerprint,
        "temp_dir": str(temp_dir or ""),
        "resources": check.exclusive_resources,
        "cleanup": cleanup,
        "blocker": "process_cleanup" if not cleanup.get("ok", True) else None,
        "evidence_path": _evidence_path(check),
    }
    if p.returncode != check.expect_exit:
        return Outcome(
            check,
            False,
            p.returncode,
            seconds,
            f"退出码 {p.returncode}，期望 {check.expect_exit}",
            combined,
            stderr,
            **common,
        )
    if check.assertion:
        ok, why = _assert(check.assertion, stdout, p.returncode)
        return Outcome(check, ok, p.returncode, seconds, why, combined, stderr, **common)
    return Outcome(check, True, p.returncode, seconds, "", combined, stderr, **common)


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


def _latest_manifest(label: str) -> Path | None:
    root = HARNESS_RUNS_DIR / _safe_name(label)
    manifests = list(root.glob("*/manifest.json")) if root.exists() else []
    return max(manifests, key=lambda p: p.stat().st_mtime) if manifests else None


def _cache_path(label: str, check: Check, fingerprint: str) -> Path:
    return HARNESS_CACHE_DIR / _safe_name(label) / _safe_name(check.id) / f"{fingerprint}.json"


def _load_reusable(
    label: str, check: Check, fingerprint: str, previous: dict[str, Any] | None
) -> Outcome | None:
    if not previous or not _cache_allowed(check):
        return None
    prior = next((x for x in previous.get("checks", []) if x.get("id") == check.id), None)
    if not prior or not prior.get("ok") or prior.get("executed") is not True:
        return None
    if prior.get("fingerprint") != fingerprint:
        return None
    path = _cache_path(label, check, fingerprint)
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if cached.get("fingerprint") != fingerprint or cached.get("ok") is not True:
        return None
    return Outcome(
        check,
        True,
        cached.get("exit"),
        0.0,
        "复用同一输入指纹的离线检查结果",
        cached.get("stdout_tail", ""),
        cached.get("stderr_tail", ""),
        executed=False,
        reused=True,
        fingerprint=fingerprint,
        temp_dir=cached.get("temp_dir", ""),
        resources=check.exclusive_resources,
        cleanup={"attempted": False, "ok": True, "reused": True},
        evidence_path=cached.get("evidence_path", ""),
    )


def _write_cache(label: str, outcome: Outcome) -> None:
    if not outcome.ok or not _cache_allowed(outcome.check) or not outcome.fingerprint:
        return
    path = _cache_path(label, outcome.check, outcome.fingerprint)
    _atomic_write_text(
        path,
        json.dumps(
            {
                "schema": 1,
                "created_at": _now(),
                "id": outcome.check.id,
                "run": outcome.check.run,
                "fingerprint": outcome.fingerprint,
                "ok": outcome.ok,
                "exit": outcome.exit,
                "seconds": round(outcome.seconds, 1),
                "stdout_sha256": hashlib.sha256(outcome.stdout.encode("utf-8")).hexdigest(),
                "stderr_sha256": hashlib.sha256(outcome.stderr.encode("utf-8")).hexdigest(),
                "stdout_tail": "\n".join(outcome.stdout.splitlines()[-12:]),
                "stderr_tail": "\n".join(outcome.stderr.splitlines()[-12:]),
                "evidence_path": outcome.evidence_path,
                "temp_dir": outcome.temp_dir,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )


def _parallel_safe(check: Check) -> bool:
    return bool(check.parallel_group) and not check.live


def _requirements_ok(check: Check, results: dict[str, Outcome]) -> bool:
    return all(results.get(req) is not None and results[req].ok for req in check.requires)


def _print_outcome(label: str, outcome: Outcome) -> None:
    if outcome.reused:
        mark = "↻"
    else:
        mark = "✔" if outcome.ok else "✖"
    note = _success_note(outcome.stdout or "") if outcome.ok else ""
    suffix = "（reused）" if outcome.reused else f"（{outcome.seconds:.1f}s）"
    print(
        f"{mark} {label} {outcome.check.id}{suffix}{('：' + outcome.detail) if outcome.detail else ''}{note}",
        flush=True,
    )
    if not outcome.ok:
        tail = "\n".join((outcome.stdout or outcome.stderr or "").strip().splitlines()[-25:])
        if tail:
            print("    " + tail.replace("\n", "\n    "))


def run_checks(
    checks: list[Check],
    label: str,
    *,
    only: list[str] | None = None,
    from_id: str | None = None,
    fail_fast: bool = False,
    resume: bool = False,
    jobs: int = 1,
) -> list[Outcome]:
    if jobs < 1:
        raise UsageError("--jobs 必须是大于 0 的整数")
    all_ids = [c.id for c in checks]
    if only and from_id:
        raise UsageError("--only 与 --from 不能同时使用")
    if only:
        unknown = [x for x in only if x not in all_ids]
        if unknown:
            raise UsageError(f"检查项不存在：{', '.join(unknown)}")
        selected = [c for c in checks if c.id in set(only)]
    elif from_id:
        if from_id not in all_ids:
            raise UsageError(f"检查项不存在：{from_id}")
        selected = checks[all_ids.index(from_id) :]
    else:
        selected = list(checks)
    if not selected:
        raise UsageError("没有可运行的检查项")

    global _LAST_RUN_MANIFEST
    run_id = f"{dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:10]}"
    run_dir = HARNESS_RUNS_DIR / _safe_name(label) / run_id
    tmp_root = run_dir / "tmp"
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    _LAST_RUN_MANIFEST = manifest_path
    previous_path = _latest_manifest(label)
    previous: dict[str, Any] | None = None
    if previous_path and previous_path != manifest_path:
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            if previous.get("status") == "running":
                previous["status"] = "stale"
                previous["blocker"] = "previous_run_left_running"
                _atomic_write_text(previous_path, json.dumps(previous, ensure_ascii=False, indent=2) + "\n")
        except (OSError, json.JSONDecodeError):
            previous = None
    mode = "diagnostic" if (only or from_id or fail_fast) else ("resume" if resume else "formal")
    manifest: dict[str, Any] = {
        "schema": 2,
        "task": label,
        "run_id": run_id,
        "mode": mode,
        "started_at": _now(),
        "status": "running",
        "jobs": jobs,
        "only": only or [],
        "from": from_id,
        "fail_fast": fail_fast,
        "checks": [],
    }
    _atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    fingerprint_base = _fingerprint_base()
    results: list[Outcome] = []
    result_map: dict[str, Outcome] = {}
    pending = list(selected)
    while pending:
        first = pending[0]
        batch = [first]
        if jobs > 1 and _parallel_safe(first):
            used = set(first.exclusive_resources)
            for candidate in pending[1:]:
                if len(batch) >= jobs or not _parallel_safe(candidate):
                    continue
                if candidate.parallel_group != first.parallel_group:
                    continue
                resources = set(candidate.exclusive_resources)
                if resources & used or not _requirements_ok(candidate, result_map):
                    continue
                batch.append(candidate)
                used.update(resources)
        pending = [c for c in pending if c not in batch]

        def execute(check: Check) -> Outcome:
            fingerprint = _cache_fingerprint(check, fingerprint_base)
            reused = _load_reusable(label, check, fingerprint, previous) if resume else None
            if reused is not None:
                return reused
            if not _requirements_ok(check, result_map):
                return Outcome(
                    check,
                    False,
                    None,
                    0.0,
                    f"依赖未通过：{', '.join(check.requires)}",
                    executed=False,
                    fingerprint=fingerprint,
                    resources=check.exclusive_resources,
                    blocker="requires",
                )
            temp_dir = tmp_root / _safe_name(check.id)
            return run_check(check, temp_dir=temp_dir, fingerprint=fingerprint)

        if len(batch) == 1:
            batch_results = [execute(first)]
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(batch), thread_name_prefix="harness"
            ) as pool:
                futures = [pool.submit(execute, c) for c in batch]
                batch_results = [f.result() for f in futures]
        for outcome in batch_results:
            if outcome.temp_dir and not outcome.reused:
                try:
                    shutil.rmtree(outcome.temp_dir, ignore_errors=False)
                    outcome.cleanup["temp_dir_removed"] = True
                except FileNotFoundError:
                    outcome.cleanup["temp_dir_removed"] = True
                except OSError as exc:
                    outcome.cleanup["temp_dir_removed"] = False
                    outcome.cleanup["temp_dir_error"] = str(exc)
                    outcome.blocker = "temp_cleanup"
                    outcome.ok = False
                    outcome.detail = (
                        outcome.detail + "; " if outcome.detail else ""
                    ) + f"临时目录清理失败：{exc}"
            _write_cache(label, outcome)
            result_map[outcome.check.id] = outcome
            results.append(outcome)
            manifest["checks"].append(outcome.evidence())
            _atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            _print_outcome(label, outcome)
        if fail_fast and any(not r.ok for r in batch_results):
            break

    manifest["status"] = (
        "passed" if results and len(results) == len(selected) and all(r.ok for r in results) else "failed"
    )
    manifest["finished_at"] = _now()
    manifest["summary"] = {
        "selected": len(selected),
        "completed": len(results),
        "failed": sum(1 for r in results if not r.ok),
        "executed": sum(1 for r in results if r.executed),
        "reused": sum(1 for r in results if r.reused),
    }
    _atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
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
    out = _git_raw("status", "--porcelain", "--untracked-files=all")
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


def _working_tree_digest() -> str:
    payload = {rel: _file_digest(rel) for rel in sorted(_changed_files())}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _review_risk(rel: str) -> str:
    low = rel.lower()
    high_risk = (
        "plan/",
        "spec/",
        "protected.lock",
        "state.json",
        "live",
        "budget",
        "credential",
        "permission",
        "auth",
        "governance",
        "security",
        ".env",
        "docker",
        "runtime",
        "evidence",
        "tasks.yaml",
        "gates.yaml",
    )
    return "high" if any(x in low for x in high_risk) else "ordinary"


def _task_owners(rel: str, tasks: dict[str, Task]) -> list[str]:
    return [t.id for t in tasks.values() if matches_any(rel, t.paths)]


def _commit_task_hint(subject: str) -> str | None:
    match = re.search(r"\b(M\d+(?:-\d+)?)\b", subject)
    return match.group(1) if match else None


def _review_artifact_paths(task_id: str) -> list[Path]:
    root = HARNESS_REVIEWS_DIR / _safe_name(task_id)
    return sorted(root.glob("*.json")) if root.exists() else []


def _implementation_identity() -> str:
    return (
        os.environ.get("MAO_IMPLEMENTER_ID")
        or os.environ.get("HARNESS_AGENT_ID")
        or os.environ.get("CODEX_AGENT_ID")
        or "implementation-agent"
    )


def _validate_review_artifact(task: Task, record: dict[str, Any], artifact: Path) -> dict[str, Any]:
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "path": str(artifact.relative_to(ROOT)),
            "decision": "needs_human",
            "reason": f"invalid_json:{exc}",
        }
    outside = list(record.get("outside_paths") or [])
    changed = data.get("changed_files")
    changed_map = changed if isinstance(changed, dict) else {}
    reasons: list[str] = []
    if data.get("task_id") != task.id:
        reasons.append("task_id_mismatch")
    if data.get("source_commit") != record.get("commit"):
        reasons.append("source_commit_mismatch")
    if data.get("working_tree_digest") != _working_tree_digest():
        reasons.append("working_tree_digest_mismatch")
    if sorted(data.get("outside_paths") or []) != sorted(outside):
        reasons.append("outside_paths_mismatch")
    if not isinstance(changed, dict):
        reasons.append("changed_files_must_be_digest_map")
    else:
        for rel in outside:
            if changed_map.get(rel) != _file_digest(rel):
                reasons.append(f"digest_mismatch:{rel}")
    ownership = data.get("file_ownership")
    tests = data.get("tests")
    if not isinstance(ownership, dict) or any(rel not in ownership for rel in outside):
        reasons.append("missing_file_ownership")
    if (
        not isinstance(tests, list)
        or not tests
        or not all(isinstance(x, dict) and x.get("passed") is True for x in tests)
    ):
        reasons.append("missing_test_evidence")
    reviewer_id = str(data.get("reviewer_id") or "")
    if not reviewer_id or reviewer_id == _implementation_identity() or reviewer_id == "implementation-agent":
        reasons.append("reviewer_not_independent")
    if (
        not data.get("reviewer_model")
        or not data.get("reviewer_context_id")
        or not data.get("reviewer_input_digest")
    ):
        reasons.append("missing_reviewer_identity")
    if data.get("decision") != "accepted":
        reasons.append(f"decision:{data.get('decision') or 'missing'}")
    high_risk = [rel for rel in outside if _review_risk(rel) == "high"]
    human_approved: list[str] = []
    if high_risk:
        approval = data.get("human_approval")
        approval_reasons: list[str] = []
        if not isinstance(approval, dict):
            approval_reasons.append("human_approval_missing")
        else:
            approver_id = str(approval.get("approver_id") or "").strip()
            approver_subject = approver_id.removeprefix("human:")
            approved_paths = approval.get("approved_paths")
            approved_path_set = set(approved_paths) if isinstance(approved_paths, list) else set()
            if approval.get("approved") is not True:
                approval_reasons.append("human_approval_not_confirmed")
            if not approver_id.startswith("human:"):
                approval_reasons.append("human_approver_identity_required")
            if approver_id == reviewer_id or approver_subject in {
                _implementation_identity(),
                "implementation-agent",
            }:
                approval_reasons.append("human_approval_not_independent")
            if approval.get("approved_source_commit") != record.get("commit"):
                approval_reasons.append("human_approval_source_commit_mismatch")
            if approval.get("approved_working_tree_digest") != _working_tree_digest():
                approval_reasons.append("human_approval_working_tree_digest_mismatch")
            if not isinstance(approved_paths, list) or not approved_paths:
                approval_reasons.append("human_approval_paths_missing")
            elif not approved_path_set.issubset(set(outside)):
                approval_reasons.append("human_approval_paths_outside_record")
            missing_paths = sorted(set(high_risk) - approved_path_set)
            if missing_paths:
                approval_reasons.append("human_approval_paths_incomplete:" + ",".join(missing_paths))
            if not str(approval.get("approval_context_id") or "").strip():
                approval_reasons.append("human_approval_context_missing")
            if not str(approval.get("approved_at") or "").strip():
                approval_reasons.append("human_approval_timestamp_missing")
            if not str(approval.get("reason") or "").strip():
                approval_reasons.append("human_approval_reason_missing")
            if not approval_reasons:
                human_approved = sorted(approved_path_set)
        if approval_reasons:
            reasons.append("high_risk_requires_human:" + ",".join(high_risk))
            reasons.extend(approval_reasons)
    decision = "accepted" if not reasons else "needs_human"
    return {
        "path": artifact.relative_to(ROOT).as_posix(),
        "decision": decision,
        "reason": ";".join(reasons),
        "reviewer_id": reviewer_id,
        "high_risk": high_risk,
        "human_approved_high_risk": human_approved,
    }


def _review_status(task: Task, state: dict[str, Any]) -> dict[str, Any]:
    record = state["tasks"].get(task.id, {})
    outside = list(record.get("outside_paths") or [])
    artifacts = [_validate_review_artifact(task, record, p) for p in _review_artifact_paths(task.id)]
    accepted = [x for x in artifacts if x.get("decision") == "accepted"]
    if not outside:
        return {"status": "not_required", "outside_paths": [], "artifacts": artifacts, "blockers": []}
    if accepted:
        independent_paths = sorted(
            {
                rel
                for item in accepted
                for rel in outside
                if rel not in set(item.get("human_approved_high_risk") or [])
            }
        )
        human_paths = sorted({rel for item in accepted for rel in item.get("human_approved_high_risk") or []})
        return {
            "status": "accepted",
            "outside_paths": outside,
            "artifacts": artifacts,
            "blockers": [],
            "accepted_by": {
                "independent_reviewer": independent_paths,
                "human_approval": human_paths,
            },
        }
    return {
        "status": "needs_human",
        "outside_paths": outside,
        "artifacts": artifacts,
        "blockers": ["outside_paths_require_independent_review"],
    }


def _review_audit(task: Task, state: dict[str, Any]) -> dict[str, Any]:
    tasks = load_tasks()
    record = state["tasks"].get(task.id, {})
    changed_files = set(_changed_files())
    review = _review_status(task, state)
    accepted_by = review.get("accepted_by") or {}
    independent_paths = set(accepted_by.get("independent_reviewer") or [])
    human_paths = set(accepted_by.get("human_approval") or [])
    rows = []
    for rel in record.get("outside_paths") or []:
        owners = _task_owners(rel, tasks)
        last_commit = _git("log", "-1", "--format=%h %ad %s", "--date=short", "--", rel)
        rows.append(
            {
                "path": rel,
                "exists": (ROOT / rel).exists(),
                "current_digest": _file_digest(rel),
                "last_commit": last_commit,
                "last_commit_task_hint": _commit_task_hint(last_commit),
                "working_tree_modified": rel in changed_files,
                "possible_task_owners": owners,
                "risk": _review_risk(rel),
                "recommendation": (
                    "needs_human"
                    if not (ROOT / rel).exists() or not owners
                    else "accepted_after_human_approval"
                    if rel in human_paths
                    else "accepted_after_independent_review"
                    if rel in independent_paths
                    else "needs_human"
                ),
            }
        )
    return {
        "schema": 1,
        "task_id": task.id,
        "task_status": status_of(state, task.id),
        "record_commit": record.get("commit"),
        "working_tree_digest": _working_tree_digest(),
        "outside_paths": list(record.get("outside_paths") or []),
        "files": rows,
        "review": review,
    }


def cmd_review(a: argparse.Namespace) -> int:
    if a.review_cmd != "audit":
        raise UsageError(a.review_cmd)
    tasks, state = load_tasks(), load_state()
    task = tasks.get(a.id)
    if task is None:
        raise UsageError(f"任务 {a.id} 不存在")
    print(json.dumps(_review_audit(task, state), ensure_ascii=False, sort_keys=True, indent=2))
    return EXIT_OK


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
    only = [x.strip() for x in (a.only or "").split(",") if x.strip()] or None
    diagnostic = bool(only or a.from_id or a.fail_fast)
    results = run_checks(
        t.accept,
        t.id,
        only=only,
        from_id=a.from_id,
        fail_fast=a.fail_fast,
        resume=a.resume,
        jobs=a.jobs,
    )
    complete_selection = len(results) == len(t.accept) and {r.check.id for r in results} == {
        c.id for c in t.accept
    }
    ok = complete_selection and all(r.ok for r in results)
    record.update({"updated_at": _now(), **_commit(), "outside_paths": outside})
    if diagnostic:
        record.setdefault("diagnostic_runs", []).append(
            {
                "manifest": str(_LAST_RUN_MANIFEST.relative_to(ROOT).as_posix())
                if _LAST_RUN_MANIFEST
                else "",
                "mode": "diagnostic",
                "complete_selection": complete_selection,
                "evidence": [r.evidence() for r in results],
            }
        )
        # 诊断结果即使全绿也不能改变正式任务状态。
        record["status"] = "in_progress"
    else:
        record.update(
            {"status": "done" if ok else "in_progress", "evidence": [r.evidence() for r in results]}
        )
    save_state(state)
    if diagnostic:
        print(f"{'✔ 诊断检查通过' if ok else '✖ 诊断检查未通过'}：{t.id} {t.title}（未改变正式任务状态）")
        return EXIT_OK if ok else EXIT_FAIL
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
            review = _review_status(t, state)
            if review["status"] == "accepted":
                print(f"✔ {t.id} 越界改动已有独立 reviewer artifact 通过校验")
            else:
                print(
                    f"⚠ {t.id} 改动了允许范围以外的文件（需要独立 reviewer 或人工审查）：{', '.join(outside)}"
                )
                for item in review.get("artifacts") or []:
                    if item.get("decision") != "accepted":
                        print(f"    review {item.get('path')}: {item.get('reason', 'needs_human')}")
    checks = [Check.parse(c, f"gate {a.milestone}", i) for i, c in enumerate(g.get("checks") or [])]
    results = run_checks(checks, a.milestone)
    review_blockers = []
    for t in tasks.values():
        if t.milestone != a.milestone:
            continue
        review = _review_status(t, state)
        if review["status"] == "needs_human":
            review_blockers.append(f"{t.id}: outside_paths")
    ok = not undone and not violations and not review_blockers and all(r.ok for r in results)
    if undone:
        print(f"✖ 尚未完成：{', '.join(undone)}")
    if review_blockers:
        print(f"✖ 越界改动审查未完成：{', '.join(review_blockers)}")
    state["gates"][a.milestone] = {
        "status": "passed" if ok else "failed",
        "updated_at": _now(),
        **_commit(),
        "evidence": [r.evidence() for r in results],
        "review": {
            "status": "passed" if not review_blockers else "needs_human",
            "blockers": review_blockers,
        },
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


ACCEPT_FILE_RE = re.compile(r"`(tests/acceptance/[\w/]+?/test_\w+\.py)`")
TEST_NAME_RE = re.compile(r"`(test_\w+)`")
ACCEPT_RUN_RE = re.compile(r"harness\.py pytest (tests/acceptance/\S+)")
PASSED_RE = re.compile(r"out\.passed >= (\d+)")


def _test_defs(path: Path) -> list[str]:
    """验收文件里的顶层测试函数名（不导入文件，避免 importorskip 把问题藏起来）。"""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]


def _spec_acceptance_tables() -> dict[str, dict[str, list[str]]]:
    """spec/system/*.md 第 7 章：验收文件 → 规范列出的测试名。表格行里的测试名归属最近一次出现的文件。"""
    out: dict[str, dict[str, list[str]]] = {}
    for p in sorted((ROOT / "spec" / "system").glob("*.md")):
        if p.name == "README.md":
            continue
        text = p.read_text(encoding="utf-8")
        m = re.search(r"^##\s*7\s*[.、]?.*$", text, re.M)
        if not m:
            continue
        end = re.search(r"^##\s*8\s*[.、]?", text[m.end() :], re.M)
        chapter = text[m.end() : m.end() + end.start()] if end else text[m.end() :]
        current: str | None = None
        files: dict[str, list[str]] = {}
        for line in chapter.splitlines():
            found = ACCEPT_FILE_RE.findall(line)
            if found:
                current = found[-1]
                files.setdefault(current, [])
            if current and line.lstrip().startswith("|"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                for cell in cells[:2]:
                    names = TEST_NAME_RE.findall(cell)
                    if names:
                        files[current].extend(n for n in names if n not in files[current])
                        break
        out[p.relative_to(ROOT).as_posix()] = files
    return out


def _acceptance_problems(tasks: dict[str, Any], gates: dict[str, Any]) -> list[str]:
    """规范第 7 章、验收文件、tasks.yaml / gates.yaml 的门槛三者必须一致。"""
    out: list[str] = []
    claimed: dict[str, str] = {}
    for spec, files in _spec_acceptance_tables().items():
        for rel, names in files.items():
            path = ROOT / rel
            if not path.exists():
                out.append(f"{spec} 第 7 章引用的验收文件 {rel} 不存在")
                continue
            claimed.setdefault(rel, spec)
            defs = _test_defs(path)
            for name in names:
                if name not in defs:
                    out.append(f"{spec} 第 7 章列出的 {name} 不在 {rel} 里")
            for name in defs:
                if name not in names and claimed[rel] == spec:
                    out.append(f"{rel} 的 {name} 没有写进 {spec} 第 7 章")
    for f in sorted((ROOT / "tests" / "acceptance").rglob("test_*.py")):
        rel = f.relative_to(ROOT).as_posix()
        if rel not in claimed:
            out.append(f"验收文件 {rel} 没有被任何 spec/system 文档的第 7 章引用")

    def count(target: str) -> int:
        path = ROOT / target
        files = sorted(path.rglob("test_*.py")) if path.is_dir() else [path] if path.exists() else []
        return sum(len(_test_defs(x)) for x in files)

    def checks_of() -> list[tuple[str, dict[str, Any]]]:
        items = [(f"任务 {t.id}", c) for t in tasks.values() for c in t.accept]
        for m, g in gates.items():
            items += [(f"阶段门 {m}", c) for c in g.get("checks") or []]
        return items

    for owner, c in checks_of():
        field = c.get if isinstance(c, dict) else lambda k, d=None, c=c: getattr(c, k, d)
        hit = ACCEPT_RUN_RE.search(str(field("run", "") or ""))
        if not hit:
            continue
        need = count(hit.group(1))
        cond = str(field("assert", "") or field("assertion", "") or "")
        got = PASSED_RE.search(cond)
        if need and (not got or int(got.group(1)) < need):
            have = got.group(1) if got else "无"
            out.append(
                f"{owner} 的验收项 {field('id')} 要求 passed >= {have}，但 {hit.group(1)} 有 {need} 个测试"
            )
        if need and "out.skipped == 0" not in cond:
            out.append(f"{owner} 的验收项 {field('id')} 没有要求 out.skipped == 0")
    return out


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
        # 代码里新增的默认模式也要登记进来，否则新受保护的文件永远不会进入基准。
        patterns = list(dict.fromkeys([*protected_patterns(), *DEFAULT_PROTECTED]))
        data = {
            "version": 1,
            "note": "受保护文件与一致性用例的基准。只能由人用 `uv run python tools/harness.py lock --update` 更新。",
            "updated_at": _now(),
            "updated_by": _git("config", "user.name") or "(unknown)",
            "patterns": patterns,
            "files": _protected_files(patterns),
            "cases": _case_digests(),
        }
        _atomic_write_text(LOCK_FILE, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
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
        out += _acceptance_problems(tasks, gates)
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
    ac.add_argument(
        "--only",
        help="开发诊断：只运行指定检查项，多个 id 用逗号分隔；结果不会把任务标记为完成",
    )
    ac.add_argument(
        "--from",
        dest="from_id",
        help="开发诊断：从指定检查项开始运行后续检查；结果不会把任务标记为完成",
    )
    ac.add_argument(
        "--fail-fast",
        action="store_true",
        help="开发诊断：第一个失败的检查批次后停止",
    )
    ac.add_argument(
        "--resume",
        action="store_true",
        help="恢复上一次同一任务运行；只复用输入指纹未变化的离线成功检查，live 永远重跑",
    )
    ac.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="并行进程数；只有声明 parallel_group 且资源不冲突的离线检查会并行（默认 1）",
    )
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

    rv = sub.add_parser("review", help="越界改动的只读审查诊断与 reviewer artifact 校验")
    rvs = rv.add_subparsers(dest="review_cmd", required=True)
    ra = rvs.add_parser("audit", help="只读分析任务的 outside_paths、历史来源、归属与风险")
    ra.add_argument("id")
    rv.set_defaults(func=cmd_review)

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
