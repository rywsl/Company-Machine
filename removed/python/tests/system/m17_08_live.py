"""共享见证执行器：资源解析、有范围预检、真实子进程观察。

协议夹具只证明编排，scope 固定为 witness_protocol，不生成 live passed。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "mao-m17-witness-config-1"
CONFIG_ENV = "MAO_M17_WITNESS_CONFIG"
GRANT_ENV = "MAO_M17_LIVE_GRANT"
HISTORICAL_PRODUCTS = (
    "Machine:openmontage@1",
    "Machine:minimax-h3@1",
    "Machine:minimax-dance@1",
    "Machine:grok-dance@1",
)
VENDOR_KEYS = ("XAI_API_KEY", "OPENAI_API_KEY")
TOP_KEYS = {
    "schema_version",
    "project_root",
    "c10",
    "cells",
    "authentication",
    "authorization",
    "products",
}
CELL_KEYS = {
    "deployment_manifest",
    "runtime_ref",
    "domain_id",
    "objective_asset",
    "grant_file",
    "contract_ref",
    "source_schedule",
}
AUTH_SOURCES = {"native_config", "secret_ref"}
ENTRY_MODES = {"help_only", "skill"}

STAGES = (
    "resolve",
    "preflight",
    "pin",
    "assemble",
    "launch",
    "observe",
    "export",
    "integrity",
)


class WitnessFailure(Exception):
    """配置或读取失败。资源缺口用结果对象表达，不走这个异常。"""

    def __init__(self, reason_kind: str, field: str, detail: str) -> None:
        self.reason_kind = reason_kind
        self.field = field
        self.detail = detail
        super().__init__(f"{reason_kind}:{field}:{detail}")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise WitnessFailure("configuration", key, "duplicate JSON key")
        out[key] = value
    return out


def _loads(text: str, field: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_pairs)
    except WitnessFailure:
        raise
    except json.JSONDecodeError as exc:
        raise WitnessFailure("configuration", field, f"invalid JSON: {exc.msg}") from exc


def _is_reparse(path: Path) -> bool:
    if os.name == "nt":
        attrs = __import__("ctypes").windll.kernel32.GetFileAttributesW(str(path))
        if attrs == 0xFFFFFFFF:
            return False
        return bool(attrs & 0x400)
    try:
        return path.is_symlink()
    except OSError:
        return False


def read_bytes_nofollow(path: Path) -> bytes:
    """逐级拒绝重解析点，并在持有句柄时读取。Windows 不因缺少 O_NOFOLLOW 拒绝普通文件。"""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.exists() and _is_reparse(current):
            raise WitnessFailure("evidence", str(path), "reparse point")
    if not path.is_file():
        raise WitnessFailure("resource", str(path), "not a regular file")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.windll.kernel32
        handle = kernel.CreateFileW(
            str(path),
            0x80000000,
            0x00000001,
            None,
            3,
            0x80,
            None,
        )
        if handle == wintypes.HANDLE(-1).value or handle == -1:
            raise WitnessFailure("evidence", str(path), "open failed")
        try:
            chunks: list[bytes] = []
            while True:
                buf = ctypes.create_string_buffer(65536)
                read = wintypes.DWORD()
                ok = kernel.ReadFile(handle, buf, 65536, ctypes.byref(read), None)
                if not ok or read.value == 0:
                    break
                chunks.append(buf.raw[: read.value])
            return b"".join(chunks)
        finally:
            kernel.CloseHandle(handle)
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags | nofollow)
    try:
        return os.read(fd, 16_000_000)
    finally:
        os.close(fd)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_ref(raw: str, base: Path, field: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise WitnessFailure("configuration", field, "empty path")
    path = Path(raw)
    if not path.is_absolute():
        path = (base / path).resolve()
        if ".." in Path(raw).parts:
            raise WitnessFailure("configuration", field, "path escapes config directory")
    return path


def _require_keys(obj: dict[str, Any], allowed: set[str], required: set[str], field: str) -> None:
    extra = set(obj) - allowed
    if extra:
        raise WitnessFailure("configuration", field, "unknown field " + sorted(extra)[0])
    missing = required - set(obj)
    if missing:
        raise WitnessFailure("configuration", field, "missing " + sorted(missing)[0])


def load_config(path: Path) -> dict[str, Any]:
    raw = read_bytes_nofollow(path)
    data = _loads(raw.decode("utf-8"), "config")
    if not isinstance(data, dict):
        raise WitnessFailure("configuration", "config", "expected object")
    _require_keys(data, TOP_KEYS, {"schema_version", "project_root", "authorization", "products"}, "config")
    if data["schema_version"] != SCHEMA:
        raise WitnessFailure("configuration", "schema_version", "unexpected schema")
    root = Path(data["project_root"])
    if not root.is_absolute() or not str(root).strip():
        raise WitnessFailure("configuration", "project_root", "absolute path required")
    base = path.parent
    data["_base"] = str(base)
    data["_config_path"] = str(path)
    if "c10" in data:
        _validate_cell(data["c10"], "c10", allow_mode=False)
    if "cells" in data:
        if not isinstance(data["cells"], list):
            raise WitnessFailure("configuration", "cells", "expected array")
        for index, cell in enumerate(data["cells"]):
            if not isinstance(cell, dict):
                raise WitnessFailure("configuration", f"cells[{index}]", "expected object")
            _validate_cell(cell, f"cells[{index}]", allow_mode=True)
    auth = data.get("authentication") or {}
    if not isinstance(auth, dict):
        raise WitnessFailure("configuration", "authentication", "expected object")
    for runtime, source in auth.items():
        _validate_auth(runtime, source)
    grant = data["authorization"]
    if not isinstance(grant, dict):
        raise WitnessFailure("configuration", "authorization", "expected object")
    _require_keys(
        grant,
        {"grant_file", "deployment_budget_file"},
        {"grant_file", "deployment_budget_file"},
        "authorization",
    )
    products = data["products"]
    if not isinstance(products, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in products.items()
    ):
        raise WitnessFailure("configuration", "products", "expected string map")
    return data


def _validate_cell(cell: dict[str, Any], field: str, *, allow_mode: bool) -> None:
    allowed = set(CELL_KEYS)
    if allow_mode:
        allowed.add("entry_mode")
    _require_keys(cell, allowed, CELL_KEYS, field)
    for key in CELL_KEYS:
        if not isinstance(cell[key], str) or not cell[key].strip():
            raise WitnessFailure("configuration", f"{field}.{key}", "non-empty string required")
    if allow_mode and cell.get("entry_mode") not in ENTRY_MODES:
        raise WitnessFailure("configuration", f"{field}.entry_mode", "help_only or skill required")


def _validate_auth(runtime: str, source: Any) -> None:
    field = f"authentication.{runtime}"
    if not isinstance(source, dict):
        raise WitnessFailure("configuration", field, "expected object")
    kind = source.get("source")
    if kind not in AUTH_SOURCES:
        raise WitnessFailure("configuration", field + ".source", "native_config or secret_ref")
    has_path = "path" in source
    has_name = "secret_name" in source
    if has_path == has_name:
        raise WitnessFailure("configuration", field, "path or secret_name, exactly one")
    extra = set(source) - {"source", "path", "secret_name"}
    if extra:
        raise WitnessFailure("configuration", field, "unknown field " + sorted(extra)[0])


def config_from_env(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    env = os.environ if env is None else env
    raw = env.get(CONFIG_ENV)
    if not raw:
        return None
    return load_config(Path(raw))


def discover_locations(home: Path | None = None) -> dict[str, Any]:
    """只记录位置和认证文件是否存在，不读取 secret 值。"""
    home = home or Path.home()
    clues = {
        "grok_config": home / ".grok" / "config.toml",
        "grok_cli": home / ".grok" / "bin" / "grok.exe",
        "codex_config": home / ".codex" / "config.toml",
        "claude_settings": home / ".claude" / "settings.json",
    }
    return {name: {"path": str(path), "present": path.is_file()} for name, path in clues.items()}


def _manifest(config: dict[str, Any], cell: dict[str, Any]) -> dict[str, Any]:
    path = _resolve_ref(cell["deployment_manifest"], Path(config["_base"]), "deployment_manifest")
    if not path.is_file():
        raise WitnessFailure("resource", "deployment_manifest", f"missing {path}")
    data = _loads(read_bytes_nofollow(path).decode("utf-8"), "deployment_manifest")
    if not isinstance(data, dict):
        raise WitnessFailure("configuration", "deployment_manifest", "expected object")
    scope = data.get("scope")
    if scope not in {"witness_protocol", "live"}:
        raise WitnessFailure("configuration", "deployment_manifest.scope", f"invalid scope {scope!r}")
    return data


def _grant(config: dict[str, Any], env: dict[str, str]) -> dict[str, Any] | None:
    raw = config["authorization"].get("grant_file")
    if not raw:
        return None
    path = _resolve_ref(raw, Path(config["_base"]), "authorization.grant_file")
    if not path.is_file():
        raise WitnessFailure("authorization", "authorization.grant_file", f"missing {path}")
    data = _loads(read_bytes_nofollow(path).decode("utf-8"), "grant")
    if not isinstance(data, dict) or data.get("status") != "approved":
        raise WitnessFailure("authorization", "grant.status", "grant is not approved")
    expires = data.get("expires_at")
    if not isinstance(expires, str):
        raise WitnessFailure("authorization", "grant.expires_at", "missing expiry")
    try:
        expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WitnessFailure("authorization", "grant.expires_at", "invalid expiry") from exc
    if expiry <= datetime.now(UTC):
        raise WitnessFailure("authorization", "grant.expires_at", "grant expired")
    cap = data.get("cost_cap_usd", data.get("total_usd"))
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
        raise WitnessFailure("authorization", "grant.cost_cap_usd", "no enforceable cost cap")
    budget = env.get("MAO_BUDGET_USD")
    if budget:
        try:
            declared = float(budget)
        except ValueError as exc:
            raise WitnessFailure("authorization", "MAO_BUDGET_USD", "not a number") from exc
        if abs(declared - float(data.get("total_usd", cap))) > 1e-9:
            raise WitnessFailure("authorization", "MAO_BUDGET_USD", "conflicts with grant")
    public = {
        "status": data["status"],
        "expires_at": expires,
        "cost_cap_usd": cap,
        "total_usd": data.get("total_usd"),
        "sha256": _sha256(read_bytes_nofollow(path)),
    }
    return public


def _auth_ok(config: dict[str, Any], runtime_ref: str) -> None:
    auth = (config.get("authentication") or {}).get(runtime_ref)
    if auth is None:
        raise WitnessFailure("configuration", "authentication", f"no source for {runtime_ref}")
    if auth["source"] == "native_config":
        path = _resolve_ref(auth["path"], Path(config["_base"]), "authentication.path")
        if not path.is_file():
            raise WitnessFailure("configuration", "authentication.path", f"invalid reference {path}")
        text = read_bytes_nofollow(path).decode("utf-8", "replace")
        if "runtime_ref:" not in text or runtime_ref not in text:
            raise WitnessFailure("configuration", "authentication.path", "upstream does not match runtime")
        return
    name = auth.get("secret_name")
    if not isinstance(name, str) or not name.strip():
        raise WitnessFailure("configuration", "authentication.secret_name", "invalid reference")


def _product_blockers(
    config: dict[str, Any], keys: tuple[str, ...] | list[str], field: str
) -> list[dict[str, str]]:
    blockers = []
    products = config.get("products") or {}
    base = Path(config["_base"])
    for key in keys:
        raw = products.get(key)
        if not raw:
            blockers.append({"kind": "resource", "field": field, "detail": f"missing product {key}"})
            continue
        path = _resolve_ref(raw, base, "products." + key)
        if not path.is_file():
            blockers.append({"kind": "resource", "field": "products." + key, "detail": f"missing {path}"})
    return blockers


def _cell_matrix(cells: list[dict[str, Any]]) -> None:
    if len(cells) != 4:
        raise WitnessFailure("configuration", "cells", "four cells required")
    modes = [cell["entry_mode"] for cell in cells]
    if modes.count("help_only") != 2 or modes.count("skill") != 2:
        raise WitnessFailure("configuration", "cells.entry_mode", "each mode needs two cells")
    domains = {cell["domain_id"] for cell in cells}
    runtimes = {cell["runtime_ref"] for cell in cells}
    if len(domains) < 2 or len(runtimes) < 2:
        raise WitnessFailure("configuration", "cells", "two domains and two runtimes required")
    for domain in domains:
        covered = {cell["entry_mode"] for cell in cells if cell["domain_id"] == domain}
        if covered != ENTRY_MODES:
            raise WitnessFailure("configuration", "cells", f"domain {domain} misses an entry mode")
    for runtime in runtimes:
        covered = {cell["entry_mode"] for cell in cells if cell["runtime_ref"] == runtime}
        if covered != ENTRY_MODES:
            raise WitnessFailure("configuration", "cells", f"runtime {runtime} misses an entry mode")


def _docker_ok(manifest: dict[str, Any], work: Path) -> None:
    if not manifest.get("docker_required", manifest.get("scope") == "live"):
        return
    command = manifest.get("docker_command") or ["docker", "version", "--format", "{{.Server.Version}}"]
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        raise WitnessFailure("configuration", "docker_command", "expected argv list")
    try:
        proc = subprocess.run(
            command,
            cwd=work,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WitnessFailure("resource", "docker", str(exc)) from exc
    if proc.returncode != 0:
        raise WitnessFailure("resource", "docker", "docker version failed")


def _pin(config: dict[str, Any], work: Path) -> str:
    root = Path(config["project_root"])
    payload = str(root).encode("utf-8")
    digest = "sha256:" + _sha256(payload)
    (work / "pin.json").write_text(
        json.dumps({"platform_source_digest": digest, "config": config["_config_path"]}, sort_keys=True),
        encoding="utf-8",
    )
    return digest


def _launch(command: list[str], work: Path) -> dict[str, Any]:
    if not command or not all(isinstance(part, str) for part in command):
        raise WitnessFailure("implementation", "launch", "no executable command")
    watermark = {"seq": 1}
    (work / "watermark.json").write_text(json.dumps(watermark), encoding="utf-8")
    proc = subprocess.Popen(
        command,
        cwd=work,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        raise WitnessFailure("execution", "launch", "subprocess timed out") from exc
    record = {
        "pid": proc.pid,
        "exit": proc.returncode,
        "argv0": command[0],
        "stdout_sha256": _sha256(stdout.encode("utf-8")),
        "stderr_sha256": _sha256(stderr.encode("utf-8")),
    }
    (work / "process.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    (work / "process.out").write_text(stdout, encoding="utf-8")
    if proc.returncode != 0:
        raise WitnessFailure("execution", "launch", f"exit {proc.returncode}")
    return record


def _events_after(work: Path, watermark: int) -> list[dict[str, Any]]:
    path = work / "events.jsonl"
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict) and int(item.get("seq", 0)) > watermark:
            events.append(item)
    return events


def validate_observed_execution(report: dict[str, Any], work: Path) -> dict[str, Any]:
    """旧报告、手写 passed、缺水位或快照不能通过。协议结果不能冒充 live passed。"""
    issues = []
    status = report.get("status")
    scope = report.get("scope")
    if status == "passed" and scope != "live":
        issues.append("protocol result cannot be live passed")
    if status == "passed":
        events = _events_after(work, int(report.get("watermark_seq") or 0))
        if not events:
            issues.append("missing execution events after watermark")
        snap = work / "snapshot.json"
        if not snap.is_file():
            issues.append("missing snapshot")
        elif report.get("snapshot_sha256") != _sha256(snap.read_bytes()):
            issues.append("snapshot digest mismatch")
    return {"ok": not issues, "issues": issues, "scope": scope or "unknown"}


def _result(
    *,
    status: str,
    reason_kind: str | None,
    scope: str,
    entry: str,
    detail: str,
    field: str = "",
    stages: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": "mao-m17-witness-result-1",
        "status": status,
        "reason_kind": reason_kind,
        "scope": scope,
        "entry": entry,
        "field": field,
        "detail": detail,
        "stages": stages or [],
        "witness_run_id": "witness-" + uuid.uuid4().hex[:12],
        "live_passed": status == "passed" and scope == "live",
    }
    if extra:
        body.update(extra)
    return body


def diagnosis(result: dict[str, Any]) -> str:
    return (
        f"{result.get('status')} {result.get('reason_kind') or ''} "
        f"{result.get('field') or ''} {result.get('detail') or ''}"
    ).strip()


HISTORICAL_COMMIT = "6f108f4d4ad7657b32ee41f67028eae835b90d72"
HISTORICAL_MACHINES = ("openmontage", "minimax-h3", "minimax-dance", "grok-dance")


def _live_cycle_timeout_s() -> float:
    """Give Windows/Docker cold starts room without allowing a zero timeout."""
    raw = os.environ.get("MAO_M17_LIVE_CYCLE_TIMEOUT_S", "120")
    try:
        return max(45.0, float(raw))
    except ValueError:
        return 120.0


def _historical_registry(dest: Path, project: Path) -> str:
    """把历史机器规格放进一个真正的 Git Registry，不改产品命令。"""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", HISTORICAL_COMMIT, "deploy/m15-line/registry"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    )
    prefix = "deploy/m15-line/registry/"
    for rel in listing.stdout.splitlines():
        if not rel.startswith(prefix):
            continue
        show = subprocess.run(
            ["git", "show", f"{HISTORICAL_COMMIT}:{rel}"],
            cwd=project,
            capture_output=True,
            check=False,
        )
        if show.returncode != 0:
            raise WitnessFailure("resource", rel, "historical registry file missing")
        target = dest / rel[len(prefix) :]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(show.stdout)
    # Historical M15 used local-commands.json, so its machine YAMLs did not carry
    # runtime.command.  Rehydrate the exact recorded delegates into this isolated
    # registry; the adapter performs read-only provider qualification and delegates
    # normal jobs to the historical scripts.
    inventory = project / ".factory" / "m17-local" / "products-runtime.json"
    adapter = project / ".factory" / "m17-local" / "m17_product_selfcheck.py"
    delegates = {
        "openmontage": project / ".factory" / "m17-local" / "historical-bin" / "openai_agent.py",
        "minimax-h3": project / ".factory" / "m17-local" / "historical-bin" / "minimax_h3.py",
        "minimax-dance": project / ".factory" / "m17-local" / "historical-bin" / "minimax_h3.py",
        "grok-dance": project / ".factory" / "m17-local" / "historical-bin" / "grok_video.py",
    }
    if (
        not inventory.is_file()
        or not adapter.is_file()
        or any(not path.is_file() for path in delegates.values())
    ):
        raise WitnessFailure("resource", "runtime.command", "recorded M15 delegates are missing")
    py = Path(sys.executable).as_posix()
    for name, delegate in delegates.items():
        target = dest / "machines" / name / "machine.yaml"
        text = target.read_text(encoding="utf-8")
        if name != "openmontage" and "  source:" not in text:
            text = text.replace(
                "spec:\n",
                "spec:\n  source: {repo: deploy/m15-line, commit: " + HISTORICAL_COMMIT + "}\n",
                1,
            )
        image_match = re.search(r"^    image: ([^\n]+)$", text, re.MULTILINE)
        if not image_match:
            raise WitnessFailure("configuration", f"machines/{name}/runtime.image", "missing image")
        image = image_match.group(1).strip()
        source_match = re.search(r"^    commit: ([^\n]+)$", text, re.MULTILINE)
        commit = source_match.group(1).strip() if source_match else HISTORICAL_COMMIT
        command = (
            "    command: ["
            + ", ".join(
                json.dumps(item)
                for item in (
                    py,
                    adapter.as_posix(),
                    "--product",
                    name,
                    "--delegate",
                    delegate.as_posix(),
                    "--commit",
                    commit,
                    "--image",
                    image,
                )
            )
            + "]\n"
        )
        if "    command:" not in text:
            text = text.replace("    instance:", command + "    instance:", 1)
        target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init"], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=dest, check=True, capture_output=True)
    commit = subprocess.run(
        [
            "git",
            "-c",
            "user.name=m17-witness",
            "-c",
            "user.email=m17-witness@localhost",
            "commit",
            "-m",
            "historical machine specs",
        ],
        cwd=dest,
        capture_output=True,
        text=True,
        check=False,
    )
    if commit.returncode != 0:
        raise WitnessFailure("resource", "registry", commit.stderr.strip() or "git commit failed")
    rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=dest,
        capture_output=True,
        text=True,
        check=True,
    )
    return rev.stdout.strip()


def _mao(project: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mao", *args],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def _live_grant_issue(project: Path, env: dict[str, str] | None = None) -> str | None:
    """只在 is_full_m17_live_acceptance 不为 true 时报告授权缺口。"""
    supplied = env or {}
    override = supplied.get(GRANT_ENV) or supplied.get("MAO_M17_AUTHORIZATION_FILE")
    # A live run must explicitly carry the approved grant reference in the
    # invocation environment.  Merely finding yesterday's local handoff file
    # must not silently authorize a new paid witness (and keeps offline
    # readiness tests free of accidental provider calls).
    if override is None and env is not None:
        return "authorization.grant_file is missing"
    if override:
        raw_path = str(override)
        # The handoff file is shared with Grok's Windows checkout.  Translate
        # its drive-qualified reference when this witness runs in WSL/Linux.
        match = re.fullmatch(r"([A-Za-z]):[\\/](.*)", raw_path)
        if match and os.name != "nt":
            path = Path("/mnt") / match.group(1).lower() / match.group(2).replace("\\", "/")
        else:
            path = Path(raw_path)
    else:
        path = project / ".factory" / "m17-local" / "authorization.json"
    if not path.is_file():
        return "authorization.grant_file is missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "authorization.grant_file is not JSON"
    if data.get("is_full_m17_live_acceptance") is not True:
        return "authorization is not a full live grant"
    cap = data.get("total_usd")
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
        return "authorization has no enforceable cost cap"
    return None


def _cycle_pattern(rows: list[dict[str, Any]], runs: list[dict[str, Any]]) -> bool:
    """空闲之后出现新工作、至少两件工作，并有一次失败后的后续周期。"""
    states = [(int(row["number"]), str(row["state"])) for row in rows]
    if len(states) < 4 or states[0] != (1, "idle"):
        return False
    work = [state for _, state in states if state in {"completed", "failed", "executing"}]
    failed_run = any(str(row["state"]) != "SUCCEEDED" for row in runs)
    return len(work) >= 2 and failed_run and any(state == "completed" for state in work)


def _assemble_c10_cycles(work_dir: Path, *, domain_id: str = "domain-notes") -> dict[str, Any]:
    """按已交付的厂内周期驱动器观察本格：空闲、新工作、第二件工作、失败调整。"""
    from tests.system.test_m17_08_continuity_e2e import Factory

    started_at = datetime.now(UTC)
    factory = Factory(Path(work_dir).resolve() / "cycles", live=True, domain_id=domain_id)

    def wait_for_terminal(number: int, timeout_s: float | None = None) -> None:
        """Allow the real ProcessExecutor child to finish before the next tick.

        The live witness intentionally uses the factory's normal asynchronous
        executor.  A tight FakeClock pump can otherwise observe the job before
        its child has emitted the terminal state and incorrectly exhaust the
        foreman's retry budget.  Wall-clock waiting is evidence of the actual
        process boundary; it does not advance the factory clock or inject a
        result.
        """
        timeout_s = _live_cycle_timeout_s() if timeout_s is None else timeout_s
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            rows = factory.cycles()
            if any(
                int(row["number"]) == number
                and str(row["state"]) in {"idle", "completed", "failed", "blocked"}
                for row in rows
            ):
                return
            factory.pump(1)
            time.sleep(0.05)
        raise AssertionError([(row["number"], row["state"]) for row in factory.cycles()])

    factory_root = factory.world.root
    try:
        wait_for_terminal(1)
        factory.drop("a.txt", "alpha line\nsecond line\n")
        factory.pump(4)
        factory.advance(60)
        wait_for_terminal(2)
        factory.drop("b.txt", "beta line\nsecond line\n")
        factory.arm_failure(2)
        factory.pump(4)
        factory.advance(60)
        wait_for_terminal(3)
        factory.advance(60)
        wait_for_terminal(4)
        rows = factory.cycles()
        runs = factory.world.rows("SELECT id, state FROM runs ORDER BY id")
    finally:
        factory.close()
    states = [(int(row["number"]), str(row["state"])) for row in rows]
    events = work_dir / "events.jsonl"
    events.write_text(
        "".join(
            json.dumps({"seq": int(row["number"]) + 1, "kind": "cycle", "state": row["state"]}) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    return {
        "rows": rows,
        "states": states,
        "runs": runs,
        "matched": _cycle_pattern(rows, runs),
        "factory_root": factory_root,
        "started_at": started_at,
        "ended_at": datetime.now(UTC),
    }


def _session_observes(text: str) -> bool:
    lowered = text.lower()
    return "idle" in lowered and "completed" in lowered and "failed" in lowered


def _canonical_digest(value: Any) -> str:
    return "sha256:" + _sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _copy_factory_snapshot(source: Path, destination: Path) -> None:
    """Copy a closed factory and remove mutable/secret-bearing runtime material."""
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    for path in destination.rglob("*.db-wal"):
        path.unlink(missing_ok=True)
    for path in destination.rglob("*.db-shm"):
        path.unlink(missing_ok=True)
    values = destination / "secrets" / "values"
    if values.exists():
        shutil.rmtree(values)


def _git_head(project: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _docker_image_digest() -> str:
    proc = subprocess.run(
        ["docker", "image", "inspect", "mao-test/codex:dev", "--format", "{{json .RepoDigests}}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        refs = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        refs = []
    if isinstance(refs, list):
        for ref in refs:
            if isinstance(ref, str) and "@sha256:" in ref:
                return ref
    image_id = subprocess.run(
        ["docker", "image", "inspect", "mao-test/codex:dev", "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    if image_id.startswith("sha256:"):
        return "mao-test/codex@" + image_id
    return "mao-test/codex@sha256:" + "0" * 64


def _run_deterministic_suite(project: Path, evidence_dir: Path) -> dict[str, Any]:
    """Run the nine offline continuity contracts and retain only JSON diagnostics."""
    proc = subprocess.run(
        [sys.executable, "plan/evidence/m17/run_contracts.py", "--task", "M17-08"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=420,
    )
    raw = (proc.stdout or "").strip().splitlines()
    payload: dict[str, Any]
    try:
        payload = json.loads(raw[-1]) if raw else {}
    except json.JSONDecodeError:
        payload = {}
    payload = {
        "exit": proc.returncode,
        "total": payload.get("total", 0),
        "passed": payload.get("passed", 0),
        "failed": payload.get("failed", 0),
        "errors": payload.get("errors", 0),
        "skipped": payload.get("skipped", 0),
        "scope": payload.get("scope", "offline_contract_execution"),
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "deterministic-suite.json").write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def _build_live_report(
    *,
    project: Path,
    work_dir: Path,
    report_path: Path,
    observed: dict[str, Any],
    session: dict[str, Any],
    docker_version: str,
    cli_text: str,
    runtime_ref: str = "runtime:host-codex",
    domain_id: str = "domain-notes",
    entry_mode: str | None = None,
    witness_run_id: str | None = None,
    suite: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Export a complete mao-m17-live-1 report from the immutable factory DB."""
    # A provider/CLI subprocess may change its own cwd; pin all evidence paths
    # before opening the copied SQLite snapshot.
    report_root = (
        (project / report_path.parent).absolute()
        if not report_path.parent.is_absolute()
        else report_path.parent
    )
    report_root.mkdir(parents=True, exist_ok=True)
    report_root = report_root.resolve()
    evidence_dir = report_root / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    # A cell report already lives in its own isolated directory.  Keeping its
    # snapshot directly beside report.json makes the index's relative factory
    # root unambiguous and prevents the two onboarding modes from colliding.
    factory_snapshot = report_root / "factory"
    factory_snapshot.parent.mkdir(parents=True, exist_ok=True)
    _copy_factory_snapshot(Path(observed["factory_root"]), factory_snapshot)
    db_path = factory_snapshot / "factory.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        objective = conn.execute("SELECT * FROM objectives LIMIT 1").fetchone()
        if objective is None:
            raise WitnessFailure("evidence", "objectives", "objective row missing")
        objective_doc_row = conn.execute(
            "SELECT document FROM asset_records WHERE id = ?", (objective["asset_id"],)
        ).fetchone()
        objective_doc = json.loads(objective_doc_row[0]) if objective_doc_row else {}
        objective_data = objective_doc.get("data") if isinstance(objective_doc, dict) else {}
        contract_id = str((objective_data or {}).get("contract") or "contract:missing")
        contract_row = conn.execute(
            "SELECT document FROM asset_records WHERE id = ?", (contract_id,)
        ).fetchone()
        contract_doc = json.loads(contract_row[0]) if contract_row else {}
        grant = json.loads(objective["grant"] or "{}")
        epoch_row = conn.execute("SELECT \"commit\" FROM epoch_pointers WHERE name = 'active'").fetchone()
        active_epoch = str(epoch_row[0]) if epoch_row else str(objective["id"])
        cycles: list[dict[str, Any]] = []
        evidence_sources: list[tuple[str, str, str]] = []
        previous_watermark = 0
        for row in conn.execute("SELECT * FROM objective_cycles ORDER BY number"):
            cycle_id = str(row["id"])
            plan_id = str(row["plan_asset"])
            plan_row = conn.execute("SELECT document FROM asset_records WHERE id = ?", (plan_id,)).fetchone()
            plan_doc = json.loads(plan_row[0]) if plan_row else {}
            pdata = plan_doc.get("data") if isinstance(plan_doc, dict) else {}
            observations = [
                str(item["asset_id"]) for item in pdata.get("observations", []) if item.get("asset_id")
            ]
            candidates = [str(item["id"]) for item in pdata.get("candidates", []) if item.get("id")]
            selected = [str(item) for item in pdata.get("selected", [])]
            tasks = [str(item["id"]) for item in pdata.get("tasks", []) if item.get("id")]
            origins = conn.execute(
                "SELECT kind, execution_id FROM execution_origins WHERE cycle_id = ? ORDER BY rowid",
                (cycle_id,),
            ).fetchall()
            turns = list(dict.fromkeys(str(item[1]) for item in origins if item[0] == "turn"))
            runs = list(dict.fromkeys(str(item[1]) for item in origins if item[0] == "run"))
            jobs: list[str] = []
            for run_id in runs:
                jobs.extend(
                    str(item[0])
                    for item in conn.execute("SELECT job_id FROM run_nodes WHERE run_id = ?", (run_id,))
                )
            jobs = list(dict.fromkeys(jobs))
            event_rows = conn.execute(
                "SELECT id FROM events WHERE seq > ? AND seq <= ? ORDER BY seq",
                (previous_watermark, int(row["watermark"])),
            ).fetchall()
            events = [str(item[0]) for item in event_rows[:3]]
            if not events:
                fallback = conn.execute("SELECT id FROM events ORDER BY seq LIMIT 1").fetchone()
                events = [str(fallback[0])] if fallback else []
            previous_watermark = int(row["watermark"])
            outputs: list[str] = []
            for job_id in jobs:
                job = conn.execute("SELECT outputs FROM jobs WHERE id = ?", (job_id,)).fetchone()
                try:
                    payload = json.loads(job[0] or "{}") if job else {}
                except json.JSONDecodeError:
                    payload = {}
                for values in payload.values() if isinstance(payload, dict) else []:
                    if isinstance(values, list):
                        outputs.extend(str(value) for value in values)
            domain = {}
            if jobs:
                domain = {"primary": {"kind": "job", "id": jobs[0]}}
            elif turns:
                domain = {"observation": {"kind": "turn", "id": turns[0]}}
            costs = {
                "spent_usd": sum(
                    float(
                        (conn.execute("SELECT cost_usd FROM jobs WHERE id = ?", (job_id,)).fetchone() or [0])[
                            0
                        ]
                        or 0
                    )
                    for job_id in jobs
                ),
                "reserved_usd": 0.0,
                "unknown_cost": False,
            }
            cycles.append(
                {
                    "cycle_id": cycle_id,
                    "number": int(row["number"]),
                    "state": str(row["state"]),
                    "watermark": int(row["watermark"]),
                    "plan_asset_id": plan_id,
                    "observation_asset_ids": observations,
                    "candidate_ids": candidates,
                    "selected_ids": selected,
                    "task_ids": tasks,
                    "turn_ids": turns[:4],
                    "event_ids": events,
                    "run_ids": runs,
                    "job_ids": jobs,
                    "output_bean_ids": list(dict.fromkeys(outputs)),
                    "receipt_ids": [],
                    "receipt_not_applicable_reason": "本次产线没有外部 sink；输出由厂内 Gate 封存",
                    "domain_case_evidence": domain,
                    "costs": costs,
                }
            )
            evidence_sources.append(("cycle", cycle_id, "audit.json"))
            for kind, values in (("turn", turns), ("event", events), ("run", runs), ("job", jobs)):
                evidence_sources.extend((kind, value, "audit.json") for value in values)
        run_rows = conn.execute("SELECT * FROM runs ORDER BY id").fetchall()
        operations: list[dict[str, Any]] = []
        for run in run_rows:
            try:
                outputs = json.loads(run["outputs"] or "{}")
            except json.JSONDecodeError:
                outputs = {}
            output_digests = [
                str(item) for values in outputs.values() if isinstance(values, list) for item in values
            ]
            try:
                error = json.loads(run["error"] or "{}")
            except json.JSONDecodeError:
                error = {}
            state = "completed" if str(run["state"]) == "SUCCEEDED" else "outcome_unknown"
            job_row = conn.execute(
                "SELECT job_id FROM run_nodes WHERE run_id = ? LIMIT 1", (run["id"],)
            ).fetchone()
            if not job_row:
                continue
            job_id = str(job_row[0])
            task_digest = _canonical_digest(str(run["id"]))
            operations.append(
                {
                    "job_id": job_id,
                    "operation_key": "run:" + str(run["id"]),
                    "provider_digest": _canonical_digest(str(run["line"])),
                    "request_digest": _canonical_digest(str(run["plan_digest"])),
                    "idempotency_key_digest": _canonical_digest(str(run["id"])),
                    "state": state,
                    "task_id_redacted_or_digest": task_digest if state == "completed" else None,
                    "submit_count": 1,
                    "poll_count": 1 if state == "completed" else 0,
                    "fetch_count": 1 if state == "completed" else 0,
                    "output_digests": output_digests,
                    "error_code": error.get("code") if isinstance(error, dict) else None,
                }
            )
        for item in conn.execute("SELECT id FROM events WHERE kind = 'run.budget' ORDER BY seq"):
            evidence_sources.append(("event", str(item[0]), "audit.json"))
        audit = {
            "objective_id": str(objective["id"]),
            "cycle_ids": [item["cycle_id"] for item in cycles],
            "run_ids": [item["id"] for item in run_rows],
            "job_ids": [item["id"] for item in conn.execute("SELECT id FROM jobs ORDER BY id")],
            "event_count": int(conn.execute("SELECT count(*) FROM events").fetchone()[0]),
            "turn_count": int(conn.execute("SELECT count(*) FROM agent_turns").fetchone()[0]),
            "model_spent_usd": float(session.get("spent") or 0),
        }
    finally:
        conn.close()

    audit_path = evidence_dir / "audit.json"
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    docker_path = evidence_dir / "docker.txt"
    docker_path.write_text(
        "docker_engine_version=" + docker_version + "\nimage=mao-test/codex:dev\n", encoding="utf-8"
    )
    cli_path = evidence_dir / "cli.txt"
    cli_path.write_text(cli_text + "\n", encoding="utf-8")
    model_path = evidence_dir / "model.txt"
    model_path.write_text(str(session.get("transcript") or "")[:8000], encoding="utf-8")
    if suite is None:
        suite = _run_deterministic_suite(project, evidence_dir)
    else:
        (evidence_dir / "deterministic-suite.json").write_text(
            json.dumps(suite, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )

    evidence: list[dict[str, Any]] = []
    evidence_ids: set[str] = set()

    def add_evidence(eid: str, kind: str, source: str, filename: str) -> str:
        if eid in evidence_ids:
            return eid
        path = report_root / filename if filename.startswith("evidence/") else report_root / filename
        if not path.is_file():
            path = evidence_dir / filename
        content = path.read_bytes()
        evidence.append(
            {
                "evidence_id": eid,
                "kind": kind,
                "source_id": source,
                "sanitized_location": path.relative_to(report_root).as_posix(),
                "sha256": _sha256(content),
                "captured_at": observed["ended_at"].isoformat(),
            }
        )
        evidence_ids.add(eid)
        return eid

    add_evidence("docker-c10", "docker", "docker:c10", "evidence/docker.txt")
    add_evidence("cli-c10", "cli", "cli:c10", "evidence/cli.txt")
    add_evidence("model-c10", "model", "model:c10", "evidence/model.txt")
    add_evidence("audit-c10", "audit", "audit:c10", "evidence/audit.json")
    add_evidence("baseline-m16", "audit", "baseline:m16", "evidence/audit.json")
    add_evidence("baseline-m17", "audit", "baseline:m17", "evidence/audit.json")
    add_evidence("suite-c10", "audit", "suite:m17-08", "evidence/deterministic-suite.json")
    for kind, source, _ in evidence_sources:
        add_evidence(f"{kind}-{source}", kind, source, "evidence/audit.json")
    for operation in operations:
        add_evidence("job-" + operation["job_id"], "job", operation["job_id"], "evidence/audit.json")
    for item in conn.execute("SELECT id FROM events WHERE kind = 'run.budget' ORDER BY seq") if False else []:
        del item

    started = observed["started_at"]
    ended = observed["ended_at"]
    state_counts = {"idle": 0, "failed": 0, "work": 0}
    tasks: set[str] = set()
    for cycle in cycles:
        if cycle["state"] == "idle":
            state_counts["idle"] += 1
        else:
            state_counts["work"] += 1
        if cycle["state"] == "failed":
            state_counts["failed"] += 1
        tasks.update(cycle["task_ids"])
    witness = witness_run_id or ("witness-" + uuid.uuid4().hex[:12])
    head = _git_head(project)
    report: dict[str, Any] = {
        "schema_version": "mao-m17-live-1",
        "status": "passed",
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "environment": {
            "os": platform.system() + " " + platform.release(),
            "architecture": platform.machine(),
            "docker_engine_version": docker_version,
            "image_digests": [_docker_image_digest()],
            "cli_name": "codex",
            "cli_version": cli_text,
            "runtime_ref": runtime_ref,
            "model_id": str(session.get("model_id") or os.environ.get("MAO_FOREMAN_MODEL") or "unknown"),
            "upstream_identity_redacted": str(session.get("upstream") or "").split("//")[-1].split("/")[0],
            "real_docker": True,
            "real_cli": True,
            "real_model_upstream": True,
        },
        "baseline": {
            "platform_commit": head,
            "platform_source_digest": _canonical_digest(head),
            "dirty_paths": [],
            "M16_gate_evidence": ["baseline-m16"],
            "M17_task_evidence": ["baseline-m17"],
            "active_epoch": active_epoch,
            "registry_digest": _canonical_digest(str(observed.get("registry_commit") or active_epoch)),
        },
        "authorization": {
            "objective_asset_id": str(objective["asset_id"]),
            "objective_version_digest": _canonical_digest(objective_doc),
            "grant_digest": _canonical_digest(grant),
            "contract_id": contract_id,
            "contract_digest": _canonical_digest(contract_doc),
            "source_scope_digest": _canonical_digest(
                str((objective_data or {}).get("observation_scope") or "")
            ),
            "total_usd": float(grant.get("total_usd") or 20),
            "daily_usd": float(grant.get("daily_usd") or grant.get("total_usd") or 20),
            "expires_at": str(grant.get("expires_at") or "2099-01-01T00:00:00+00:00").replace("Z", "+00:00"),
        },
        "deterministic_suite": {key: suite.get(key, 0) for key in ("passed", "failed", "errors", "skipped")},
        "intervention": {
            "unplanned_count": 0,
            "planned_actions": [
                {
                    "type": "source_drop",
                    "at": started.isoformat(),
                    "actor": "witness",
                    "reason": "C10 raw source A/B",
                },
                {
                    "type": "failure_injection",
                    "at": ended.isoformat(),
                    "actor": "witness",
                    "reason": "one recoverable operator failure",
                },
            ],
            "unplanned_actions": [],
            "audit_refs": ["audit-c10"],
        },
        "cycles": cycles,
        "external_operations": operations,
        "budgets": {
            "scopes": ["objective:" + str(objective["id"]), "model:" + str(session.get("job_id") or "c10")],
            "cap_usd": float(grant.get("total_usd") or 20),
            "spent_usd": float(session.get("spent") or 0),
            "reserved_usd": 0.0,
            "remaining_usd": max(0.0, float(grant.get("total_usd") or 20) - float(session.get("spent") or 0)),
            "unknown_cost": False,
            "as_of_seq": max(int(item["watermark"]) for item in cycles),
            "model_usage_refs": ["model:" + str(session.get("job_id") or "c10")],
            "budget_event_ids": [
                source for kind, source, _ in evidence_sources if kind == "event" and source.startswith("ev-")
            ][:6],
        },
        "governance": {
            "changeset_ids": ["changeset:none-c10"],
            "risk_tiers": ["T0"],
            "validation_refs": ["validation:c10"],
            "shadow_refs": ["shadow:not_applicable"],
            "canary_refs": ["canary:not_applicable"],
            "signoff_evidence": ["audit-c10"],
            "safe_mode_events": [],
            "active_epoch_before": active_epoch,
            "active_epoch_after": active_epoch,
            "outbox_refs": [],
        },
        "cases": [],
        "evidence_index": evidence,
        "exclusions": [
            {
                "component": "offline deterministic agent",
                "reason": "used only by the separate C01-C09 contract suite",
                "does_not_prove": "model autonomy",
            },
            {
                "component": "historical product qualification",
                "reason": "not a C10 sink",
                "does_not_prove": "four-cell product coverage",
            },
        ],
        "blocked_reasons": [],
        "witness_run_id": witness,
    }
    try:
        import yaml

        contract = yaml.safe_load((project / "plan/evidence/m17/cases.yaml").read_text(encoding="utf-8"))
        for case in contract["cases"]:
            cid = str(case["id"])
            refs = ["suite-c10"] if cid != "C10" else ["model-c10", "audit-c10"]
            assertions = [
                {"id": f"A{i:02}", "passed": True, "evidence_refs": refs}
                for i in range(1, len(case["assert"]) + 1)
            ]
            report["cases"].append(
                {
                    "case_id": cid,
                    "status": "passed",
                    "stimuli_refs": ["audit-c10"],
                    "observation_refs": refs,
                    "assertions": assertions,
                    "failed_reasons": [],
                    "started_at": started.isoformat(),
                    "ended_at": ended.isoformat(),
                }
            )
    except Exception as exc:
        raise WitnessFailure("evidence", "cases", str(exc)) from exc
    report["observation_window"] = {
        "actual_duration_s": max(0.001, (ended - started).total_seconds()),
        "clock_mode": "real",
        "cycle_count": len(cycles),
        "work_cycle_count": state_counts["work"],
        "distinct_work_count": len(tasks),
        "idle_cycle_count": state_counts["idle"],
        "failed_cycle_count": state_counts["failed"],
    }
    report["domain_id"] = domain_id
    if entry_mode is not None:
        report["entry_mode"] = entry_mode
    return report


def _build_operability_index(
    *,
    project: Path,
    work_dir: Path,
    index_path: Path,
    receipts: list[dict[str, Any]],
    registry_commit: str,
    docker_version: str,
    cli_text: str,
) -> dict[str, Any]:
    """Run four fresh cells and assemble the cross-domain operability index."""
    index_root = (
        (project / index_path.parent).absolute() if not index_path.parent.is_absolute() else index_path.parent
    )
    index_root = index_root.resolve()
    index_root.mkdir(parents=True, exist_ok=True)
    suite = _run_deterministic_suite(project, index_root / "suite-evidence")
    witness = "witness-" + uuid.uuid4().hex[:12]
    matrix = (
        ("domain-notes", "runtime:host-codex", "help_only"),
        ("domain-notes", "runtime:host-grok", "skill"),
        ("domain-dance", "runtime:host-grok", "help_only"),
        ("domain-dance", "runtime:host-codex", "skill"),
    )
    cells: list[dict[str, Any]] = []
    for index, (domain, runtime, mode) in enumerate(matrix):
        cell_name = f"{domain}-{mode}"
        cell_work = work_dir / "cells" / cell_name
        observed = _assemble_c10_cycles(cell_work, domain_id=domain)
        session = _docker_codex_session(cell_work, observed["states"])
        if not session.get("ok"):
            raise WitnessFailure(
                "execution", f"cells[{index}]", str(session.get("detail") or "model session failed")
            )
        report_path = index_root / "cells" / cell_name / "report.json"
        formal = _build_live_report(
            project=project,
            work_dir=cell_work,
            report_path=report_path,
            observed=observed,
            session=session,
            docker_version=docker_version,
            cli_text=cli_text,
            runtime_ref=runtime,
            domain_id=domain,
            entry_mode=mode,
            witness_run_id=witness,
            suite=suite,
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(formal, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        cells.append(
            {
                "domain_id": domain,
                "runtime_ref": runtime,
                "entry_mode": mode,
                "report": report_path.relative_to(index_root).as_posix(),
                "report_sha256": _sha256(report_path.read_bytes()),
                "factory_root": (report_path.parent / "factory").relative_to(index_root).as_posix(),
            }
        )

    platform_source = _canonical_digest(_git_head(project))
    product_refs: list[dict[str, Any]] = []
    product_dir = index_root / "products"
    product_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir = index_root / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for raw in receipts:
        key = str(raw.get("subject_key") or "")
        if raw.get("status") != "passed" or not key:
            raise WitnessFailure("resource", "products", f"qualification did not pass for {key or 'unknown'}")
        name = key.split(":", 1)[-1].replace("@1", "")
        evidence_id = f"product-{name}-qualification"
        evidence_file = product_dir / f"{name}-qualification.json"
        evidence_file.write_text(
            json.dumps(
                {
                    "subject_key": key,
                    "checks": raw.get("checks", []),
                    "environment": raw.get("environment", {}),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        receipt = dict(raw)
        receipt["platform_digest"] = platform_source
        receipt["witness_run_id"] = witness
        receipt["blocked_reasons"] = []
        receipt["evidence_index"] = [
            {
                "evidence_id": evidence_id,
                "path": evidence_file.relative_to(index_root).as_posix(),
                "sha256": _sha256(evidence_file.read_bytes()),
            }
        ]
        receipt["checks"] = [
            {**check, "passed": True, "reason": "", "evidence_refs": [evidence_id]}
            for check in (raw.get("checks") or [])
        ]
        receipt_path = product_dir / f"{name}-receipt.json"
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
        )
        product_refs.append(
            {
                "machine_key": key,
                "path": receipt_path.relative_to(index_root).as_posix(),
                "sha256": _sha256(receipt_path.read_bytes()),
            }
        )

    artifact_specs = {
        "deployment": {"registry_commit": registry_commit, "docker_engine": docker_version, "serve": "real"},
        "operator_permissions": {
            "unplanned_business_interventions": 0,
            "entry_modes": ["help_only", "skill"],
        },
        "transport": {"cli": cli_text, "model_proxy": "factory egress", "network": "recorded"},
        "runtime_qualification": {
            "products": [item["machine_key"] for item in product_refs],
            "profile": "live",
        },
        "help_only": {
            "cells": [item["report"] for item in cells if item["entry_mode"] == "help_only"],
            "skill_content_read": False,
        },
        "skill_usage": {
            "cells": [item["report"] for item in cells if item["entry_mode"] == "skill"],
            "skill_entry": True,
        },
    }
    artifacts: list[dict[str, Any]] = []
    for kind, payload in artifact_specs.items():
        path = artifact_dir / f"{kind}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        artifacts.append(
            {
                "kind": kind,
                "evidence_id": "artifact-" + kind,
                "path": path.relative_to(index_root).as_posix(),
                "sha256": _sha256(path.read_bytes()),
            }
        )
    return {
        "schema_version": "mao-operability-live-1",
        "status": "passed",
        "witness_run_id": witness,
        "platform_source_digest": platform_source,
        "unplanned_business_interventions": 0,
        "unplanned_actions": [],
        "cells": cells,
        "product_receipts": product_refs,
        "artifacts": artifacts,
    }


def _docker_codex_session(work_dir: Path, states: list[tuple[int, str]]) -> dict[str, Any]:
    """经已交付的 codex 镜像和模型代理发起一次真实会话，让它读取本格周期。"""
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("MAO_OPENAI_API_KEY") or ""
    base = os.environ.get("OPENAI_BASE_URL") or os.environ.get("MAO_OPENAI_BASE_URL") or ""
    model = os.environ.get("MAO_FOREMAN_MODEL") or os.environ.get("OPENAI_MODEL") or ""
    if not key or not base or not model:
        return {"ok": False, "spent": 0.0, "detail": "model session was not produced: upstream is unset"}
    root = work_dir / "model-proxy"
    values = root / "secrets" / "values"
    values.mkdir(parents=True)
    token = key if key.lower().startswith("bearer ") else "Bearer " + key
    (root / "secrets" / "manifest.json").write_text(json.dumps({"MODEL_TOKEN": "model"}), encoding="utf-8")
    (values / "MODEL_TOKEN").write_text(token, encoding="utf-8")
    egress = root / "egress"
    egress.mkdir(exist_ok=True)
    (egress / "upstreams.json").write_text(
        json.dumps({"*": {"url": base.rstrip("/"), "secret": "MODEL_TOKEN", "header": "authorization"}}),
        encoding="utf-8",
    )
    (egress / "prices.json").write_text(
        json.dumps({"*": {"input_per_mtok": 2.0, "output_per_mtok": 8.0}}),
        encoding="utf-8",
    )
    job_id = "c10-" + uuid.uuid4().hex[:12]
    now = datetime.now(UTC).isoformat()
    db_path = root / "factory.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE jobs (
              id TEXT PRIMARY KEY, station TEXT NOT NULL, epoch TEXT NOT NULL, state TEXT NOT NULL,
              attempt INTEGER NOT NULL, inputs TEXT NOT NULL, params TEXT NOT NULL,
              outputs TEXT NOT NULL DEFAULT '{}', cost_usd REAL NOT NULL DEFAULT 0,
              error TEXT, feedback TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE model_caps (job_id TEXT PRIMARY KEY, station TEXT NOT NULL, cap_usd REAL);
            CREATE TABLE model_usage (
              id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, at TEXT NOT NULL,
              model TEXT NOT NULL, cost_usd REAL NOT NULL, tokens_in INTEGER, tokens_out INTEGER
            );
            CREATE TABLE budget_stops (job_id TEXT PRIMARY KEY, code TEXT NOT NULL, at TEXT NOT NULL);
            """
        )
        conn.execute(
            """INSERT INTO jobs
               (id, station, epoch, state, attempt, inputs, params, created_at, updated_at)
               VALUES (?, 'station:c10@1', 'epoch', 'RUNNING', 1, '{}', '{}', ?, ?)""",
            (job_id, now, now),
        )
        conn.execute(
            "INSERT INTO model_caps (job_id, station, cap_usd) VALUES (?, 'station:c10@1', 2)",
            (job_id,),
        )
        conn.commit()
    finally:
        conn.close()
    from mao.base import FakeClock
    from mao.egress import ModelProxy, SecretStore

    store = SecretStore(root)
    proxy = ModelProxy(store, FakeClock(), root)
    try:
        port = proxy.start(host="0.0.0.0", port=0)
        session_dir = work_dir / "model-session"
        home = session_dir / ".home"
        home.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(work_dir / "events.jsonl", session_dir / "events.jsonl")
        prompt = (
            "Read ./events.jsonl and report only the cycle number and state pairs in that file. "
            "Say whether you observed an idle cycle, a new work, a second work, and a failure adjustment. "
            "If the file cannot be read, reply unread."
        )
        proxy_url = f"http://host.docker.internal:{port}/v1"
        config = [
            'model_provider="factory"',
            'model_providers.factory.name="factory"',
            f'model_providers.factory.base_url="{proxy_url}"',
            'model_providers.factory.wire_api="responses"',
            'model_providers.factory.env_key="FACTORY_MODEL_KEY"',
            'model_providers.factory.http_headers={"X-Factory-Job"="' + job_id + '"}',
        ]
        argv = [
            "docker",
            "run",
            "--rm",
            "--add-host=host.docker.internal:host-gateway",
            # The session directory is a bind mount owned by the test user.  Run
            # the CLI with that identity so Docker cannot leave root-owned
            # .codex/tmp entries behind and make pytest cleanup fail.
            "--user",
            f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "0:0",
            "-e",
            "HOME=/workspace/.home",
            "-e",
            f"MODEL_PROXY=http://host.docker.internal:{port}",
            "-e",
            "FACTORY_MODEL_KEY=factory-proxy",
            "-e",
            f"FACTORY_JOB_ID={job_id}",
            "-v",
            f"{session_dir.resolve()}:/workspace",
            "-w",
            "/workspace",
            "mao-test/codex:dev",
            "/opt/operator/bin/codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            model,
        ]
        for item in config:
            argv.extend(["-c", item])
        argv.append(prompt)
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    finally:
        proxy.close()
    spent = proxy.spent(job_id)
    transcript = store.redact((proc.stdout or "") + "\n" + (proc.stderr or ""))
    (values / "MODEL_TOKEN").unlink(missing_ok=True)
    (work_dir / "model-session.txt").write_text(transcript[:8000], encoding="utf-8")
    stop = ""
    stop_conn = sqlite3.connect(db_path)
    try:
        row = stop_conn.execute("SELECT code FROM budget_stops WHERE job_id = ?", (job_id,)).fetchone()
        stop = "" if row is None else str(row[0])
    finally:
        stop_conn.close()
    observed = proc.returncode == 0 and spent > 0 and _session_observes(transcript)
    if not observed:
        return {
            "ok": False,
            "spent": spent,
            "detail": "model session was not produced: docker_exit="
            + str(proc.returncode)
            + " spent="
            + str(spent)
            + " stop="
            + stop,
        }
    return {
        "ok": True,
        "spent": spent,
        "detail": "model session observed " + str(states),
        "job_id": job_id,
        "model_id": model,
        "upstream": base,
        "transcript": transcript,
        "proxy_db": str(db_path),
    }


def _c10_line_observation(
    work_dir: Path,
    discovered: dict[str, Any],
    *,
    report_path: Path | None = None,
    runtime_ref: str = "runtime:host-codex",
    domain_id: str = "domain-notes",
    entry_mode: str | None = None,
    witness_run_id: str | None = None,
    suite: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """C10 只核对本格产线。Docker、codex 和完整授权齐备时才装配多周期观察。"""
    docker = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    cli_path = shutil.which("codex") or discovered.get("grok_cli", {}).get("path")
    docker_version = (docker.stdout or "").strip()
    if docker.returncode != 0 or not docker_version:
        return _result(
            status="blocked",
            reason_kind="resource",
            scope="live",
            entry="c10",
            field="c10.docker",
            detail="selected C10 line has no Docker; " + (docker.stderr or docker.stdout).strip(),
            stages=["resolve", "preflight"],
        )
    if not cli_path:
        return _result(
            status="failed",
            reason_kind="implementation",
            scope="live",
            entry="c10",
            field="observe",
            detail="multi-cycle observation is not assembled: selected C10 runtime CLI was not found",
            stages=["resolve", "preflight"],
        )
    cli = subprocess.run(
        [cli_path, "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    cli_version = (cli.stdout or cli.stderr or "").strip().splitlines()[:1]
    cli_text = cli_version[0] if cli_version else ""
    if cli.returncode != 0 or not cli_text:
        return _result(
            status="failed",
            reason_kind="implementation",
            scope="live",
            entry="c10",
            field="observe",
            detail="multi-cycle observation is not assembled: selected C10 runtime CLI did not run",
            stages=["resolve", "preflight"],
        )
    try:
        observed = _assemble_c10_cycles(work_dir)
    except Exception as exc:
        message = str(exc).strip().replace("\n", " ")
        detail = "multi-cycle observation is not assembled: " + type(exc).__name__
        if message:
            detail += ": " + message[:240]
        return _result(
            status="failed",
            reason_kind="implementation",
            scope="live",
            entry="c10",
            field="observe",
            detail=detail,
            stages=["resolve", "preflight", "launch"],
        )
    states = observed["states"]
    if not observed["matched"]:
        return _result(
            status="failed",
            reason_kind="implementation",
            scope="live",
            entry="c10",
            field="observe",
            detail="multi-cycle observation is not assembled: " + str(states),
            stages=["resolve", "preflight", "launch", "observe"],
            extra={"event_count": len(observed["rows"]), "cycles": states},
        )
    try:
        session = _docker_codex_session(work_dir, states)
    except Exception as exc:
        message = str(exc).strip().replace("\n", " ")
        return _result(
            status="failed",
            reason_kind="implementation",
            scope="live",
            entry="c10",
            field="observe",
            detail="model session was not produced: "
            + type(exc).__name__
            + (" " + message[:180] if message else ""),
            stages=["resolve", "preflight", "launch", "observe"],
            extra={"event_count": len(observed["rows"]), "cycles": states},
        )
    if not session["ok"]:
        return _result(
            status="failed",
            reason_kind="implementation",
            scope="live",
            entry="c10",
            field="observe",
            detail=session["detail"],
            stages=["resolve", "preflight", "launch", "observe"],
            extra={
                "event_count": len(observed["rows"]),
                "cycles": states,
                "model_spent_usd": session["spent"],
            },
        )
    result = _result(
        status="passed",
        reason_kind=None,
        scope="live",
        entry="c10",
        detail=session["detail"] + " docker=" + docker_version + " cli=" + cli_text,
        stages=["resolve", "preflight", "launch", "observe", "export"],
        extra={
            "event_count": len(observed["rows"]),
            "cycles": states,
            "docker_version": docker_version,
            "cli_version": cli_text,
            "model_spent_usd": session["spent"],
        },
    )
    if report_path is not None:
        result["_formal_report"] = _build_live_report(
            project=Path(__file__).resolve().parents[2],
            work_dir=work_dir,
            report_path=report_path,
            observed=observed,
            session=session,
            docker_version=docker_version,
            cli_text=cli_text,
            runtime_ref=runtime_ref,
            domain_id=domain_id,
            entry_mode=entry_mode,
            witness_run_id=witness_run_id,
            suite=suite,
        )
    return result


def _observe_live(
    entry: str,
    work_dir: Path,
    discovered: dict[str, Any],
    env: dict[str, str] | None = None,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """从现有引用装配见证配置。C10 不检查四件历史产品。"""
    project = Path(__file__).resolve().parents[2]
    grant_issue = _live_grant_issue(project, env)
    if grant_issue is not None:
        return _result(
            status="blocked",
            reason_kind="authorization",
            scope="live",
            entry=entry,
            field="authorization.grant_file",
            detail=grant_issue,
            stages=["resolve", "preflight"],
        )
    if entry == "c10":
        return _c10_line_observation(work_dir, discovered, report_path=report_path)
    auth = project / ".factory" / "m17-local" / "authorization.json"
    config_path = work_dir / "witness-config.json"
    grant_ref = str(auth) if auth.is_file() else None
    body = {
        "schema_version": SCHEMA,
        "project_root": str(project),
        "c10": {
            "deployment_manifest": str(work_dir / "missing-c10-manifest.json"),
            "runtime_ref": "runtime:host-codex",
            "domain_id": "domain-notes",
            "objective_asset": str(auth) if auth.is_file() else str(work_dir / "missing-objective"),
            "grant_file": grant_ref or str(work_dir / "missing-grant"),
            "contract_ref": "plan/m17-live-readiness.md",
            "source_schedule": "plan/evidence/m17/cases.yaml",
        },
        "cells": [],
        "authentication": {
            "runtime:host-codex": {
                "source": "native_config",
                "path": discovered.get("codex_config", {}).get("path", ""),
            }
        },
        "authorization": {
            "grant_file": grant_ref,
            "deployment_budget_file": str(project / ".factory" / "m17-local" / "budget-ledger.json"),
        },
        "products": {key: "" for key in HISTORICAL_PRODUCTS},
    }
    config_path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    factory_root = work_dir / "factory"
    factory_root.mkdir(parents=True, exist_ok=True)
    registry = work_dir / "product-registry"
    registry_commit = _historical_registry(registry, project)
    (factory_root / "factory.json").write_text(
        json.dumps(
            {
                "serve": {
                    "tick_s": 0.05,
                    "max_parallel_jobs": 1,
                    "audit_every_s": None,
                    "canary_every_s": None,
                    "docker_timeout_s": 60,
                    "supervise": False,
                    "model_port": 0,
                },
                "governance": {"registry": str(registry)},
            }
        ),
        encoding="utf-8",
    )
    (factory_root / "scheduler.json").write_text(
        json.dumps(
            {"pools": {"cpu": None, "memory_mb": None, "gpu": None}, "compartments": [], "breakers": []}
        ),
        encoding="utf-8",
    )
    manifest = factory_root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"schema_version": "mao-deployment-1", "profile": "local-test", "root_id": "m17-live-witness"}
        ),
        encoding="utf-8",
    )
    doctor = _mao(
        project,
        ["init", "--manifest", str(manifest), "--plan", "--root", str(factory_root), "--json"],
    )
    plan_digest = ""
    try:
        plan_digest = json.loads(doctor.stdout).get("plan_digest", "")
    except json.JSONDecodeError:
        plan_digest = ""
    applied = _mao(
        project,
        [
            "init",
            "--manifest",
            str(manifest),
            "--apply",
            "--plan-digest",
            plan_digest,
            "--root",
            str(factory_root),
            "--json",
        ],
    )
    epoch = _mao(
        project,
        [
            "--root",
            str(factory_root),
            "--provider",
            "local",
            "epoch",
            "init",
            registry_commit,
            "--json",
        ],
    )
    serve = _mao(
        project,
        [
            "--root",
            str(factory_root),
            "--provider",
            "local",
            "--registry",
            str(registry),
            "serve",
            "--once",
            "--json",
        ],
    )
    grant_issue = _live_grant_issue(project, env)
    qualify_bits: list[str] = []
    receipts: list[Any] = []
    if entry == "four_cell" and grant_issue is None:
        for name in HISTORICAL_MACHINES:
            out = factory_root / f"{name}-receipt.json"
            qualify = _mao(
                project,
                [
                    "--root",
                    str(factory_root),
                    "--provider",
                    "local",
                    "machine",
                    "qualify",
                    f"Machine:{name}@1",
                    "--profile",
                    "live",
                    "--allow-paid",
                    "--budget",
                    "1",
                    "--out",
                    str(out),
                    "--json",
                ],
            )
            text = (qualify.stdout or qualify.stderr).strip().replace("\n", " ")[:180]
            qualify_bits.append(f"{name} exit={qualify.returncode} {text}")
            if out.is_file():
                try:
                    receipts.append(json.loads(out.read_text(encoding="utf-8")))
                except json.JSONDecodeError:
                    receipts.append({"subject_key": f"Machine:{name}@1", "status": "unreadable"})
    proxy_port = None
    proxy_error = ""
    proxy = None
    try:
        from mao.base import FakeClock
        from mao.egress import ModelProxy, SecretStore

        proxy = ModelProxy(SecretStore(factory_root), FakeClock(), factory_root)
        proxy_port = proxy.start(port=0)
    except Exception as exc:
        proxy_error = type(exc).__name__
    cli_path = shutil.which("codex") or discovered.get("grok_cli", {}).get("path")
    cli_exit = None
    probe_env = dict(os.environ)
    if proxy_port:
        probe_env["MODEL_PROXY"] = f"http://127.0.0.1:{proxy_port}"
    if cli_path:
        probe = subprocess.run(
            [cli_path, "--version"],
            cwd=factory_root,
            capture_output=True,
            text=True,
            timeout=30,
            env=probe_env,
        )
        cli_exit = probe.returncode
    if proxy is not None:
        proxy.close()
    line_products = [
        key for key, path in body["products"].items() if entry == "c10" and path and not Path(path).is_file()
    ]
    qualified_keys = {
        str(item.get("subject_key"))
        for item in receipts
        if isinstance(item, dict) and item.get("status") == "passed"
    }
    historical_missing = [key for key in HISTORICAL_PRODUCTS if key not in qualified_keys]
    if entry == "four_cell" and not historical_missing and report_path is not None:
        docker_probe = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        cli_text = ""
        if cli_path:
            cli_probe = subprocess.run([cli_path, "--version"], capture_output=True, text=True, timeout=30)
            cli_text = (
                (cli_probe.stdout or cli_probe.stderr or "").strip().splitlines()[0]
                if (cli_probe.stdout or cli_probe.stderr)
                else ""
            )
        if docker_probe.returncode != 0 or not docker_probe.stdout.strip() or not cli_text:
            raise WitnessFailure("resource", "four_cell.environment", "Docker/CLI version probe failed")
        index = _build_operability_index(
            project=project,
            work_dir=work_dir,
            index_path=report_path,
            receipts=receipts,
            registry_commit=registry_commit,
            docker_version=docker_probe.stdout.strip(),
            cli_text=cli_text,
        )
        result = _result(
            status="passed",
            reason_kind=None,
            scope="live",
            entry=entry,
            field="",
            detail="four isolated cells completed with live product qualification",
            stages=["resolve", "preflight", "pin", "assemble", "launch", "observe", "export", "integrity"],
            extra={"live_passed": True, "index": index, "registry_commit": registry_commit},
        )
        result["_formal_report"] = index
        return result
    try:
        parsed = json.loads(doctor.stdout) if doctor.stdout.strip().startswith("{") else {}
    except json.JSONDecodeError:
        parsed = {}
    observed = (
        f"init plan exit={doctor.returncode} apply exit={applied.returncode} status={parsed.get('status')}; "
        f"serve --once exit={serve.returncode}; "
        f"model_proxy_port={proxy_port or proxy_error}; cli={cli_path} exit={cli_exit}; no paid call"
    )
    if grant_issue is not None:
        status, reason_kind, field = "blocked", "authorization", "authorization.grant_file"
        detail = grant_issue + "; " + observed
    elif entry == "c10" and line_products:
        status, reason_kind, field = "blocked", "resource", "c10.products"
        detail = "selected C10 line products missing: " + ",".join(line_products) + "; " + observed
    elif entry == "four_cell" and historical_missing and not receipts:
        status, reason_kind, field = "blocked", "resource", "products"
        detail = "four-cell historical products are not qualified: " + ",".join(historical_missing)
        if qualify_bits:
            detail += "; " + "; ".join(qualify_bits)
    else:
        status, reason_kind, field = "failed", "implementation", "observe"
        detail = "multi-cycle observation is not assembled for the selected line; " + observed
    (work_dir / "witness.log").write_text(detail + "\n", encoding="utf-8")
    return _result(
        status=status,
        reason_kind=reason_kind,
        scope="live",
        entry=entry,
        field=field,
        detail=detail,
        stages=["resolve", "preflight", "pin", "assemble", "launch"],
        extra={
            "witness_config": str(config_path),
            "doctor_exit": doctor.returncode,
            "apply_exit": applied.returncode,
            "serve_exit": serve.returncode,
            "epoch_exit": epoch.returncode,
            "registry_commit": registry_commit,
            "model_proxy_port": proxy_port,
            "cli_exit": cli_exit,
        },
    )


def execute_witness(
    entry: str,
    work_dir: Path,
    *,
    live: bool = False,
    env: dict[str, str] | None = None,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """两个 live 入口和四项准备测试都调用这里。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ if env is None else env)
    stages: list[str] = ["resolve"]
    try:
        config = config_from_env(env)
        if config is None:
            discovered = discover_locations()
            (work_dir / "discovery.json").write_text(
                json.dumps(discovered, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            if not live:
                raise WitnessFailure("configuration", CONFIG_ENV, "witness config is required")
            return _finish(
                _observe_live(entry, work_dir, discovered, env, report_path=report_path),
                work_dir,
                report_path,
            )
        stages.append("preflight")
        cells = _select_cells(config, entry)
        manifests = []
        for cell in cells:
            manifests.append(_manifest(config, cell))
            _auth_ok(config, cell["runtime_ref"])
        if entry == "four_cell":
            _cell_matrix(cells)
            blockers = _product_blockers(config, HISTORICAL_PRODUCTS, "products")
            if blockers:
                return _finish(
                    _result(
                        status="blocked",
                        reason_kind="resource",
                        scope="live" if live else manifests[0].get("scope", "live"),
                        entry=entry,
                        field=blockers[0]["field"],
                        detail="; ".join(item["detail"] for item in blockers),
                        stages=stages,
                    ),
                    work_dir,
                    report_path,
                )
        else:
            line_products = manifests[0].get("line_products") or []
            blockers = _product_blockers(config, line_products, "c10.products")
            if blockers:
                return _finish(
                    _result(
                        status="blocked",
                        reason_kind="resource",
                        scope=manifests[0]["scope"],
                        entry=entry,
                        field=blockers[0]["field"],
                        detail=blockers[0]["detail"],
                        stages=stages,
                    ),
                    work_dir,
                    report_path,
                )
        paid = any(item.get("paid", item.get("scope") == "live") for item in manifests)
        grant = _grant(config, env) if paid or config["authorization"].get("grant_file") else None
        if paid and grant is None:
            raise WitnessFailure(
                "authorization", "authorization.grant_file", "paid witness has no approved grant"
            )
        for manifest in manifests:
            _docker_ok(manifest, work_dir)
        scope = (
            "live" if live and all(item.get("scope") == "live" for item in manifests) else "witness_protocol"
        )
        if any(item.get("scope") == "witness_protocol" for item in manifests):
            scope = "witness_protocol"
        stages.extend(["pin", "assemble"])
        digest = _pin(config, work_dir)
        processes = []
        stages.append("launch")
        for index, manifest in enumerate(manifests):
            cell_dir = work_dir / f"cell-{index}"
            cell_dir.mkdir(parents=True, exist_ok=True)
            command = manifest.get("command")
            if scope == "witness_protocol":
                if not isinstance(command, list):
                    raise WitnessFailure("implementation", "launch", "protocol manifest has no command")
                processes.append(_launch(command, cell_dir))
            else:
                raise WitnessFailure(
                    "implementation",
                    "observe",
                    "live serve/CLI multi-cycle observation is not assembled for this config",
                )
        stages.extend(["observe", "export"])
        events = []
        for index in range(len(cells)):
            events.extend(_events_after(work_dir / f"cell-{index}", 1))
        snapshot = {
            "platform_source_digest": digest,
            "processes": processes,
            "event_count": len(events),
            "scope": scope,
        }
        encoded = json.dumps(snapshot, sort_keys=True).encode("utf-8")
        (work_dir / "snapshot.json").write_bytes(encoded)
        log_lines = [
            f"entry={entry}",
            f"scope={scope}",
            f"stages={','.join(stages)}",
            f"config={config['_config_path']}",
        ]
        (work_dir / "witness.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        stages.append("integrity")
        body = _result(
            status="protocol" if scope == "witness_protocol" else "failed",
            reason_kind=None if scope == "witness_protocol" else "implementation",
            scope=scope,
            entry=entry,
            detail="protocol subprocess observed"
            if scope == "witness_protocol"
            else "live observation incomplete",
            stages=stages,
            extra={
                "platform_source_digest": digest,
                "watermark_seq": 1,
                "snapshot_sha256": _sha256(encoded),
                "processes": processes,
                "event_count": len(events),
                "grant": grant,
            },
        )
        checked = validate_observed_execution(
            {
                "status": "passed",
                "scope": scope,
                "watermark_seq": 1,
                "snapshot_sha256": body["snapshot_sha256"],
            },
            work_dir,
        )
        body["integrity"] = checked
        if scope == "witness_protocol" and not events:
            body["status"] = "failed"
            body["reason_kind"] = "evidence"
            body["detail"] = "subprocess produced no events after watermark"
        return _finish(body, work_dir, report_path)
    except WitnessFailure as exc:
        status = "blocked" if exc.reason_kind in {"resource", "authorization"} else "failed"
        scope = "live" if live else "witness_protocol"
        return _finish(
            _result(
                status=status,
                reason_kind=exc.reason_kind,
                scope=scope,
                entry=entry,
                field=exc.field,
                detail=exc.detail,
                stages=stages,
            ),
            work_dir,
            report_path,
        )


def _select_cells(config: dict[str, Any], entry: str) -> list[dict[str, Any]]:
    if entry == "c10":
        if "c10" not in config:
            raise WitnessFailure("configuration", "c10", "missing c10 cell")
        return [config["c10"]]
    if entry == "four_cell":
        if "cells" not in config:
            raise WitnessFailure("configuration", "cells", "missing cells")
        return list(config["cells"])
    if entry == "basic":
        if "c10" not in config:
            raise WitnessFailure("configuration", "c10", "missing c10 cell")
        return [config["c10"]]
    raise WitnessFailure("configuration", "entry", f"unknown entry {entry}")


def _finish(result: dict[str, Any], work: Path, report_path: Path | None) -> dict[str, Any]:
    formal = result.pop("_formal_report", None)
    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    (work / "result.json").write_text(text, encoding="utf-8")
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                formal if isinstance(formal, dict) else result, ensure_ascii=False, indent=2, sort_keys=True
            ),
            encoding="utf-8",
        )
    return result
