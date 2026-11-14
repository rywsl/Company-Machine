"""秘密申请、核验与保存。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mao.base import canonical_json, utc_iso
from mao.errors import FactoryError

_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_CLASSES = ("publish", "ingress", "other")
_MEMORY = re.compile(r"^([0-9]+)(Mi|M|MB|Gi|G|GB)$")


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    code: str
    exit_code: int | None
    log: str


def request_secret(
    ctx: Any,
    snapshot: Any,
    *,
    name: str,
    secret_class: str,
    station: str,
    reason: str,
    origin: str,
) -> Any:
    from mao.egress import SecretStore
    from mao.inbox import Inbox

    if _NAME.fullmatch(name) is None:
        raise FactoryError("SECRET_REQUEST_INVALID", "秘密名不合法", reason="name")
    if secret_class not in _CLASSES:
        raise FactoryError("SECRET_REQUEST_INVALID", "秘密分类不合法", reason="class")
    try:
        station_key = snapshot.key("Station", station)
        station_spec = snapshot.spec(station_key)
    except FactoryError as exc:
        if exc.code == "SPEC_NOT_FOUND":
            raise FactoryError("SECRET_REQUEST_INVALID", "工位不存在", reason="station") from exc
        raise
    machine_key = snapshot.key("Machine", station_spec["machine"])
    machine = snapshot.spec(machine_key)
    checks = (machine.get("needs") or {}).get("secret_checks") or {}
    check = checks.get(name) if isinstance(checks, dict) else None
    if not isinstance(check, dict) or "timeout_s" not in check:
        raise FactoryError("SECRET_CHECK_MISSING", "没有核验配方", name=name, machine=machine_key)
    if name not in _bound_names(snapshot, station_spec):
        raise FactoryError("SECRET_REQUEST_INVALID", "秘密不在注入名单", reason="not-bound")
    role = str(station_spec.get("role") or "")
    if secret_class == "publish" and role != "sink":
        raise FactoryError("SECRET_REQUEST_INVALID", "分类与角色不相容", reason="role")
    if secret_class == "ingress" and role != "source":
        raise FactoryError("SECRET_REQUEST_INVALID", "分类与角色不相容", reason="role")
    store = SecretStore(ctx.root)
    exists = name in store.names()
    if exists and store.secret_class(name) != secret_class:
        raise FactoryError("SECRET_REQUEST_INVALID", "分类与已保存的不一致", reason="class-mismatch")
    timeout = check.get("timeout_s")
    if isinstance(timeout, float) and timeout.is_integer():
        timeout = int(timeout)
    return Inbox(ctx).open(
        "secret",
        subject=name,
        origin=origin,
        summary=reason,
        payload={
            "name": name,
            "class": secret_class,
            "station": station,
            "check": {"timeout_s": timeout},
            "exists": exists,
            "rejected": 0,
        },
    )


def verify_secret(
    ctx: Any,
    snapshot: Any,
    *,
    station: str,
    name: str,
    value: str,
    label: str,
) -> CheckResult:
    from secrets import token_hex

    from mao.egress import (
        SecretStore,
        build_egress,
        close_egress,
        credentialed_env,
        proxy_env,
        register_check_egress,
    )
    from mao.sandbox import Mount, SandboxSpec

    try:
        station_key = snapshot.key("Station", station)
        station_spec = snapshot.spec(station_key)
    except FactoryError as exc:
        if exc.code == "SPEC_NOT_FOUND":
            raise FactoryError("SECRET_REQUEST_INVALID", "工位不存在", reason="station") from exc
        raise
    machine_key = snapshot.key("Machine", station_spec["machine"])
    machine = snapshot.spec(machine_key)
    checks = (machine.get("needs") or {}).get("secret_checks") or {}
    check = checks.get(name) if isinstance(checks, dict) else None
    if not isinstance(check, dict) or not check.get("run"):
        raise FactoryError("SECRET_CHECK_MISSING", "没有核验配方", name=name, machine=machine_key)
    store = SecretStore(ctx.root)
    plan = build_egress(snapshot, station_key, store)
    if name not in plan.secret_names:
        raise FactoryError("SECRET_REQUEST_INVALID", "秘密不在注入名单", reason="not-bound")
    folder = _work_dir(ctx, label)
    workspace = folder / "workspace"
    logs = folder / "logs"
    workspace.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    stdout = logs / "check.stdout"
    stderr = logs / "check.stderr"
    others = [item for item in plan.secret_names if item != name and item in set(store.names())]
    env = store.inject(others)
    env[name] = value
    env["FACTORY_SECRET_CHECK"] = name
    network = plan.network
    registered = False
    if network == "egress":
        if getattr(ctx, "proxies", None) is None:
            from mao.egress import start_owned_proxies

            start_owned_proxies(ctx, model_port=0, timeout_s=60.0)
        token = token_hex(16)
        register_check_egress(ctx.db, label, plan, token=token)
        registered = True
        env.update(plan.env)
        proxies = getattr(ctx, "proxies", None)
        if proxies is not None:
            docker = getattr(ctx.provider, "name", "") == "docker"
            env.update(credentialed_env(proxy_env(proxies, docker=docker), label, token))
    from mao.sandbox import sandbox_resources

    resources = sandbox_resources(machine)
    spec = SandboxSpec(
        name="mao-" + label,
        image=str((machine.get("runtime") or {}).get("image") or ""),
        mounts=(
            Mount(source=str(workspace.resolve()), target="/workspace", readonly=False),
            Mount(
                source=str((ctx.root / "tools" / "factory").resolve()),
                target="/opt/factory",
                readonly=True,
            ),
        ),
        workdir="/workspace",
        env=env,
        network=network,
        cpus=resources["cpus"],
        memory_mb=resources["memory_mb"],
        shm_mb=resources["shm_mb"],
        tmp_mb=resources["tmp_mb"],
        pids=resources["pids"],
        writable=tuple(resources["writable"]),
    )
    argv = [item if isinstance(item, str) else str(item) for item in check.get("run") or []]
    timeout_s = float(check.get("timeout_s") or 0)
    sandbox = None
    result = None
    try:
        sandbox = ctx.provider.open(spec)
        result = sandbox.exec(argv, timeout_s=timeout_s, stdout=stdout, stderr=stderr, workdir="/workspace")
    except FactoryError:
        return _checked(ctx, stdout, False, "SECRET_CHECK_ERROR", None)
    finally:
        if sandbox is not None:
            sandbox.close()
        if registered:
            close_egress(ctx.db, label)
        _redact_log(stdout, value, store)
        _redact_log(stderr, value, store)
    if result is None:
        return _checked(ctx, stdout, False, "SECRET_CHECK_ERROR", None)
    if result.timed_out or result.exit_code is None:
        return _checked(ctx, stdout, False, "SECRET_CHECK_ERROR", result.exit_code)
    if result.exit_code == 0:
        return _checked(ctx, stdout, True, "", 0)
    if result.exit_code == 1:
        return _checked(ctx, stdout, False, "SECRET_INVALID", 1)
    return _checked(ctx, stdout, False, "SECRET_CHECK_ERROR", result.exit_code)


def store_secret(root: Path, name: str, secret_class: str, value: str) -> None:
    folder = Path(root) / "secrets"
    values = folder / "values"
    values.mkdir(parents=True, exist_ok=True)
    part = values / f"{name}.part"
    dest = values / name
    part.write_text(value, encoding="utf-8", newline="")
    _mode(part)
    os.replace(part, dest)
    _mode(dest)
    manifest = folder / "manifest.json"
    body: dict[str, Any] = {}
    if manifest.is_file():
        loaded = json.loads(manifest.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            body = loaded
    body[name] = secret_class
    temporary = manifest.with_name("manifest.json.part")
    temporary.write_text(json.dumps(body), encoding="utf-8")
    os.replace(temporary, manifest)


def normalize_value(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FactoryError("SECRET_VALUE_INVALID", "秘密值不是 UTF-8", reason="encoding") from exc
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    else:
        text = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    if text == "":
        raise FactoryError("SECRET_VALUE_INVALID", "秘密值为空", reason="empty")
    if len(text) > 65536:
        raise FactoryError("SECRET_VALUE_INVALID", "秘密值过长", reason="too-long")
    if "\x00" in text:
        raise FactoryError("SECRET_VALUE_INVALID", "秘密值含空字节", reason="nul")
    if "\n" in text or "\r" in text:
        raise FactoryError("SECRET_VALUE_INVALID", "秘密值含换行", reason="newline")
    return text


def fulfil_secret(ctx: Any, request: Any, raw: bytes, *, by: str) -> Any:
    from mao.inbox import Inbox

    value = normalize_value(raw)
    snapshot = station_snapshot(ctx, str(request.payload.get("station") or ""))
    if snapshot is None:
        snapshot = _active_snapshot(ctx)
    n = _next_n(ctx.root / "inbox" / request.id, "check-")
    label = f"{request.id}-check-{n}"
    result = verify_secret(
        ctx,
        snapshot,
        station=str(request.payload.get("station") or ""),
        name=request.subject,
        value=value,
        label=label,
    )
    if not result.ok:
        _bump_rejected(ctx, request.id)
        _event(
            ctx,
            "secret.rejected",
            {
                "name": request.subject,
                "request": request.id,
                "code": result.code,
                "exit_code": result.exit_code,
            },
        )
        raise FactoryError(
            result.code or "SECRET_INVALID",
            "秘密核验不通过",
            request=request.id,
            exit_code=result.exit_code,
            log=result.log,
        )
    klass = str(request.payload.get("class") or "")
    store_secret(ctx.root, request.subject, klass, value)
    _event(
        ctx,
        "secret.stored",
        {"name": request.subject, "class": klass, "by": by, "request": request.id},
    )
    return Inbox(ctx).close(
        request.id,
        status="answered",
        answer={"verified": True, "check": n},
        by=by,
    )


def remember_stored(ctx: Any, name: str, secret_class: str, *, by: str, request: str | None) -> None:
    _event(ctx, "secret.stored", {"name": name, "class": secret_class, "by": by, "request": request})


def direct_label(ctx: Any, name: str) -> str:
    prefix = f"secret-{name.lower()}-"
    return prefix + str(_next_n(ctx.root / "secrets" / "checks", prefix))


def station_snapshot(ctx: Any, station: str) -> Any | None:
    from mao.governance.epochs import EpochStore

    try:
        epochs = EpochStore(ctx)
        active = epochs.materialize(epochs.current())
    except FactoryError:
        return None
    if _has_station(active, station):
        return active
    rows = ctx.db.query(
        "SELECT \"commit\" FROM changesets WHERE state IN ('verified', 'shadow', 'canary') ORDER BY id"
    )
    for row in rows:
        snapshot = epochs.materialize(str(row["commit"]))
        if _has_station(snapshot, station):
            return snapshot
    return None


def _active_snapshot(ctx: Any) -> Any:
    from mao.governance.epochs import EpochStore

    epochs = EpochStore(ctx)
    return epochs.materialize(epochs.current())


def _has_station(snapshot: Any, station: str) -> bool:
    try:
        snapshot.doc(snapshot.key("Station", station))
    except FactoryError as exc:
        if exc.code == "SPEC_NOT_FOUND":
            return False
        raise
    return True


def _bound_names(snapshot: Any, station_spec: dict[str, Any]) -> list[str]:
    policy = snapshot.spec(snapshot.key("Policy", station_spec["policy"]))
    secrets = policy.get("secrets")
    if secrets == "from-machine":
        machine = snapshot.spec(snapshot.key("Machine", station_spec["machine"]))
        return [str(item) for item in (machine.get("needs") or {}).get("secrets") or []]
    if isinstance(secrets, list):
        return [str(item) for item in secrets]
    return []


def _work_dir(ctx: Any, label: str) -> Path:
    marker = "-check-"
    if marker in label:
        request_id, number = label.rsplit(marker, 1)
        if number.isdigit() and ctx.db.query("SELECT id FROM human_requests WHERE id = ?", (request_id,)):
            return Path(ctx.root) / "inbox" / request_id / f"check-{number}"
    return Path(ctx.root) / "secrets" / "checks" / label


def _next_n(folder: Path, prefix: str) -> int:
    highest = 0
    if folder.is_dir():
        for child in folder.iterdir():
            if not child.is_dir() or not child.name.startswith(prefix):
                continue
            suffix = child.name[len(prefix) :]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
    return highest + 1


def _checked(ctx: Any, stdout: Path, ok: bool, code: str, exit_code: int | None) -> CheckResult:
    try:
        log = stdout.resolve().relative_to(Path(ctx.root).resolve()).as_posix()
    except ValueError:
        log = stdout.as_posix()
    return CheckResult(ok, code, exit_code, log)


def _redact_log(path: Path, value: str, store: Any) -> None:
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    if value:
        text = text.replace(value, "***")
    path.write_text(store.redact(text), encoding="utf-8")


def _bump_rejected(ctx: Any, request_id: str) -> None:
    from mao.inbox import Inbox

    current = Inbox(ctx).get(request_id)
    payload = dict(current.payload)
    payload["rejected"] = int(payload.get("rejected") or 0) + 1
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE human_requests SET payload = ? WHERE id = ?",
            (canonical_json(payload), request_id),
        )


def _event(ctx: Any, kind: str, data: dict[str, Any]) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, NULL, ?)",
            (ctx.ids.new("ev"), utc_iso(ctx.clock.now()), kind, canonical_json(data)),
        )


def _resources(resources: Any) -> tuple[float | None, int | None]:
    if not isinstance(resources, dict) or not resources:
        return None, None
    cpus = resources.get("cpus")
    memory = resources.get("memory")
    memory_mb = None
    if memory is not None:
        match = _MEMORY.fullmatch(str(memory))
        if match is None:
            raise FactoryError("SANDBOX_SPEC_INVALID", "memory 无法解析", memory=str(memory))
        amount = int(match.group(1))
        memory_mb = amount if match.group(2) in ("Mi", "M", "MB") else amount * 1024
    return (None if cpus is None else float(cpus), memory_mb)


def _mode(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, 0o600)
