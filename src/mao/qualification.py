"""运行时与产品接入资格（spec/system/operability.md 第 3.2、5.3、5.4 节）。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from mao.base import canonical_json, utc_iso
from mao.errors import FactoryError

_RUNTIME_CHECKS = ("launch", "contract", "tools", "limits", "result", "resume", "fencing")
_MACHINE_CHECKS = ("identity", "health", "invoke", "outputs", "recovery", "network")


def qualify_runtime(
    ctx: Any,
    runtime_key: str,
    *,
    profile: str,
    budget_usd: float | None = None,
    allow_paid: bool = False,
) -> dict[str, Any]:
    _profile(profile, budget_usd, allow_paid)
    snapshot = _snapshot(ctx)
    spec = snapshot.spec(runtime_key)
    if spec.get("kind") != "agent-cli":
        raise FactoryError("RUNTIME_UNQUALIFIED", "只有 agent-cli 进入会话资格", reason="kind")
    work = ctx.root / "qualification" / ctx.ids.new("qual")
    work.mkdir(parents=True)
    fact = "fact-" + ctx.ids.new("qf")
    contract = "contract-" + ctx.ids.new("qc")
    fence = "fence-" + ctx.ids.new("qd")
    home = work / "session"
    home.mkdir()
    first = _run_round(
        ctx,
        snapshot,
        runtime_key,
        spec,
        task=f"记住私有事实 {fact} 并调用工具。不要把事实写进共享文件。",
        contract=f"必须在结果中原样包含 {contract}",
        session=None,
        home=home,
        fence=fence,
        work=work / "round1",
        budget_usd=0.0 if budget_usd is None else budget_usd,
    )
    session_id = str(first["session_id"] or "")
    second = _run_round(
        ctx,
        snapshot,
        runtime_key,
        spec,
        task="只回答上一轮的私有事实，不要猜测。",
        contract="回答事实即可",
        session=session_id or None,
        home=home,
        fence=fence,
        work=work / "round2",
        budget_usd=0.0 if budget_usd is None else budget_usd,
    )
    checks = _runtime_checks(first, second, fact, contract, fence, session_id)
    return _store(
        ctx,
        kind="runtime",
        subject_key=runtime_key,
        subject_digest=_runtime_digest(spec),
        profile=profile,
        status="passed" if all(item["passed"] for item in checks) else "failed",
        checks=checks,
        environment=_environment(spec, first),
        costs={"usd": float(first.get("cost_usd") or 0) + float(second.get("cost_usd") or 0)},
    )


def qualify_machine(
    ctx: Any,
    machine_key: str,
    *,
    profile: str,
    budget_usd: float | None = None,
    allow_paid: bool = False,
) -> dict[str, Any]:
    _profile(profile, budget_usd, allow_paid)
    snapshot = _snapshot(ctx)
    spec = snapshot.spec(machine_key)
    command = list((spec.get("runtime") or {}).get("command") or [])
    if not command:
        raise FactoryError("MACHINE_IDENTITY_MISMATCH", "机器没有可执行命令", reason="command")
    work = ctx.root / "qualification" / ctx.ids.new("qual")
    work.mkdir(parents=True)
    report_path = work / "report.json"
    proc = _subprocess(command + ["--self-check", str(report_path)], work, {})
    report = _read_json(report_path)
    if report.get("state") == "pending" and report.get("task_id"):
        proc = _subprocess(
            command + ["--poll", str(report["task_id"]), "--self-check", str(report_path)],
            work,
            {},
        )
        report = _read_json(report_path)
    checks = _machine_checks(spec, report, proc.returncode)
    status = "passed" if all(item["passed"] for item in checks) else "failed"
    if any(item["id"] == "identity" and not item["passed"] for item in checks):
        status = "failed"
    return _store(
        ctx,
        kind="machine",
        subject_key=machine_key,
        subject_digest=_subject_digest(spec),
        profile=profile,
        status=status,
        checks=checks,
        environment={
            "form": spec.get("form"),
            "image": (spec.get("runtime") or {}).get("image"),
            "source": spec.get("source") or {},
            "reported": report.get("identity") or {},
        },
        costs={"usd": float(report.get("cost_usd") or 0)},
    )


def receipt_valid(ctx: Any, receipt: dict[str, Any]) -> bool:
    snapshot = _snapshot(ctx)
    key = str(receipt.get("subject_key") or "")
    try:
        spec = snapshot.spec(key)
    except FactoryError:
        return False
    current = _runtime_digest(spec) if receipt.get("kind") == "runtime" else _subject_digest(spec)
    return str(receipt.get("subject_digest") or "") == current and receipt.get("status") == "passed"


def _run_round(
    ctx: Any,
    snapshot: Any,
    runtime_key: str,
    spec: dict[str, Any],
    *,
    task: str,
    contract: str,
    session: str | None,
    home: Path,
    fence: str,
    work: Path,
    budget_usd: float,
) -> dict[str, Any]:
    from mao.operator import build_runtime_invocation

    ref = runtime_key.split(":", 1)[-1]
    invocation = build_runtime_invocation(
        snapshot,
        ref,
        task=task,
        contract=contract,
        model=str((spec.get("models_allowed") or ["fake-model-1"])[0]),
        max_turns=3,
        budget_usd=budget_usd,
        timeout_s=30,
        session=session,
    )
    work.mkdir(parents=True)
    transcript = work / "transcript.txt"
    env = {
        "QUAL_HOME": str(home),
        "QUAL_FENCE": fence,
        "QUAL_MAX_TURNS": "3",
    }
    env.update(invocation.env)
    proc = _subprocess(list(invocation.argv), work, env)
    transcript.write_bytes(proc.stdout or b"")
    parsed = _parse_events(transcript.read_text(encoding="utf-8", errors="replace"))
    parsed.update(
        {
            "exit_code": proc.returncode,
            "argv": list(invocation.argv),
            "task": task,
            "contract": contract,
            "stdout": transcript.read_text(encoding="utf-8", errors="replace"),
        }
    )
    return parsed


def _runtime_checks(
    first: dict[str, Any],
    second: dict[str, Any],
    fact: str,
    contract: str,
    fence: str,
    session_id: str,
) -> list[dict[str, Any]]:
    tools = first.get("tools") or []
    paired = bool(tools) and all(item.get("id") and item.get("result_id") == item.get("id") for item in tools)
    fenced = bool(tools) and all(item.get("fence") == fence for item in tools)
    duplicate = any(item.get("duplicate") for item in tools)
    contract_ok = contract in str(first.get("result") or "") and contract not in str(first.get("task") or "")
    result_ok = bool(first.get("result")) and first.get("exit_code") == 0 and not first.get("is_error")
    limits_ok = int(first.get("turns") or 1) <= 3 and not first.get("over_turns")
    resume_ok = (
        bool(session_id)
        and fact not in str(second.get("task") or "")
        and str(second.get("result") or "").strip() == fact
        and second.get("exit_code") == 0
    )
    launch_ok = first.get("exit_code") == 0 and bool(first.get("stdout"))
    rows = [
        ("launch", launch_ok, "" if launch_ok else "进程没有成功启动"),
        ("contract", contract_ok, "" if contract_ok else "没有消费 factory_contract"),
        ("tools", paired and not duplicate, "" if paired and not duplicate else "工具调用没有成对或重复"),
        ("limits", limits_ok, "" if limits_ok else "超过轮次上限"),
        ("result", result_ok, "" if result_ok else "缺少结构化结果"),
        ("resume", resume_ok, "" if resume_ok else "不能从私有会话回忆事实"),
        ("fencing", fenced, "" if fenced else "副作用没有带上本轮 fence"),
    ]
    return [{"id": name, "passed": ok, "evidence_refs": [], "reason": reason} for name, ok, reason in rows]


def _machine_checks(spec: dict[str, Any], report: dict[str, Any], code: int) -> list[dict[str, Any]]:
    from mao.transport import resolve_service_location

    identity = report.get("identity") if isinstance(report.get("identity"), dict) else {}
    declared = spec.get("source") if isinstance(spec.get("source"), dict) else {}
    image = str((spec.get("runtime") or {}).get("image") or "")
    identity_ok = (
        code == 0
        and str(identity.get("commit") or "") == str(declared.get("commit") or "")
        and str(identity.get("image") or "") == image
        and bool(declared.get("commit"))
    )
    health_ok = code == 0 and report.get("healthy") is True
    invoke_ok = code == 0 and bool(report.get("invoked"))
    outputs_ok = bool(report.get("artifact"))
    recovery = report.get("recovery") if isinstance(report.get("recovery"), dict) else {}
    recovery_ok = recovery.get("resubmit") is False and bool(recovery.get("task_id"))
    network_ok = True
    reason = ""
    try:
        base = str(report.get("base_url") or "")
        allowed = list(report.get("allowed_origins") or [])
        if report.get("location"):
            resolve_service_location(str(report["location"]), base_url=base, allowed_origins=allowed)
        for item in report.get("redirects") or []:
            resolve_service_location(str(item), base_url=base, allowed_origins=allowed)
    except FactoryError as exc:
        network_ok = False
        reason = exc.code
    rows = [
        ("identity", identity_ok, "" if identity_ok else "声明与实际产品不一致"),
        ("health", health_ok, "" if health_ok else "健康检查失败"),
        ("invoke", invoke_ok, "" if invoke_ok else "没有实际调用"),
        ("outputs", outputs_ok, "" if outputs_ok else "没有产物"),
        ("recovery", recovery_ok, "" if recovery_ok else "未知任务被重新提交"),
        ("network", network_ok, reason),
    ]
    return [{"id": name, "passed": ok, "evidence_refs": [], "reason": why} for name, ok, why in rows]


def _store(
    ctx: Any,
    *,
    kind: str,
    subject_key: str,
    subject_digest: str,
    profile: str,
    status: str,
    checks: list[dict[str, Any]],
    environment: dict[str, Any],
    costs: dict[str, Any],
) -> dict[str, Any]:
    receipt_id = ctx.ids.new("qr")
    platform = hashlib.sha256(subject_digest.encode("utf-8")).hexdigest()
    receipt = {
        "schema_version": "mao-qualification-1",
        "id": receipt_id,
        "kind": kind,
        "subject_key": subject_key,
        "subject_digest": subject_digest,
        "platform_digest": platform,
        "profile": profile,
        "status": status,
        "environment": environment,
        "checks": checks,
        "evidence_index": [item["id"] for item in checks],
        "costs": costs,
        "blocked_reasons": [item["reason"] for item in checks if item["reason"]],
    }
    now = utc_iso(ctx.clock.now())
    with ctx.db.tx() as conn:
        conn.execute(
            """INSERT INTO qualifications (
                   id, kind, subject_key, subject_digest, profile, platform_digest,
                   environment_digest, status, receipt_digest, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                receipt_id,
                kind,
                subject_key,
                subject_digest,
                profile,
                platform,
                hashlib.sha256(canonical_json(environment).encode("utf-8")).hexdigest(),
                status,
                hashlib.sha256(canonical_json(receipt).encode("utf-8")).hexdigest(),
                now,
            ),
        )
    if status != "passed":
        code = "MACHINE_IDENTITY_MISMATCH" if kind == "machine" else "RUNTIME_UNQUALIFIED"
        failed = [item["id"] for item in checks if not item["passed"]]
        raise FactoryError(code, "资格未通过", id=receipt_id, checks=failed, receipt=receipt)
    return receipt


def _snapshot(ctx: Any) -> Any:
    from mao.governance.epochs import EpochStore

    epochs = EpochStore(ctx)
    return epochs.materialize(epochs.current())


def _profile(profile: str, budget_usd: float | None, allow_paid: bool) -> None:
    if profile not in ("offline", "live"):
        raise FactoryError("RUNTIME_UNQUALIFIED", "profile 不合法", reason="profile")
    if profile == "live" and (
        not allow_paid or not isinstance(budget_usd, (int, float)) or float(budget_usd) <= 0
    ):
        raise FactoryError("RUNTIME_UNQUALIFIED", "live 资格需要费用上界", reason="budget")


def _subprocess(argv: list[str], work: Path, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        cwd=work,
        env={**os.environ, **env},
        capture_output=True,
        timeout=20,
        check=False,
    )


def _parse_events(text: str) -> dict[str, Any]:
    tools: list[dict[str, Any]] = []
    result = ""
    session_id = ""
    is_error = False
    turns = 1
    cost = 0.0
    over_turns = False
    seen: set[str] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if isinstance(obj.get("session_id"), str):
            session_id = str(obj["session_id"])
        if obj.get("type") == "result":
            result = str(obj.get("result") or "")
            is_error = bool(obj.get("is_error"))
            turns = int(obj.get("num_turns") or 1)
            cost = float(obj.get("total_cost_usd") or 0)
        call = obj.get("tool_use") if isinstance(obj.get("tool_use"), dict) else None
        if call is None and obj.get("type") == "tool_call":
            call = obj
        if call is not None:
            call_id = str(call.get("id") or "")
            tools.append(
                {
                    "id": call_id,
                    "result_id": "",
                    "fence": call.get("fence"),
                    "duplicate": call_id in seen,
                }
            )
            seen.add(call_id)
            if call.get("over_turns"):
                over_turns = True
        if obj.get("type") == "tool_result":
            answered = str(obj.get("tool_use_id") or obj.get("call_id") or "")
            for item in tools:
                if item["id"] == answered and not item["result_id"]:
                    item["result_id"] = answered
    return {
        "tools": tools,
        "result": result,
        "session_id": session_id,
        "is_error": is_error,
        "turns": turns,
        "cost_usd": cost,
        "over_turns": over_turns,
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


def _runtime_digest(spec: dict[str, Any]) -> str:
    argv = list((spec.get("invoke") or {}).get("argv") or [])
    extra = ""
    if len(argv) >= 2 and Path(str(argv[1])).is_file():
        extra = hashlib.sha256(Path(str(argv[1])).read_bytes()).hexdigest()
    return hashlib.sha256((canonical_json(spec) + extra).encode("utf-8")).hexdigest()


def _subject_digest(spec: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


def _environment(spec: dict[str, Any], first: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": spec.get("kind"),
        "models_allowed": list(spec.get("models_allowed") or []),
        "transcript": (spec.get("transcript") or {}).get("format"),
        "session_id": first.get("session_id"),
    }
