#!/usr/bin/env python3
"""受控厂内代理：只读原始资料，并通过 factoryctl MCP 完成选择、派工和评价。

测试驱动不向本程序注入候选、任务参数或模型答复。来源路径来自目标资产的
observation_scope；候选和任务标识由本程序根据读到的文件摘要生成。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def log(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()
    workspace = os.environ.get("FACTORY_WORKSPACE")
    if not workspace:
        return
    path = Path(workspace) / "notebook"
    path.mkdir(parents=True, exist_ok=True)
    with (path / "agent.log").open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def notebook() -> dict:
    workspace = os.environ.get("FACTORY_WORKSPACE") or "."
    path = Path(workspace) / "notebook" / "state.json"
    if not path.is_file():
        return {"seen": [], "failed": [], "done": []}
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"seen": [], "failed": [], "done": []}
    if not isinstance(body, dict):
        return {"seen": [], "failed": [], "done": []}
    body.setdefault("seen", [])
    body.setdefault("failed", [])
    body.setdefault("done", [])
    return body


def save_notebook(body: dict) -> None:
    workspace = os.environ.get("FACTORY_WORKSPACE") or "."
    path = Path(workspace) / "notebook"
    path.mkdir(parents=True, exist_ok=True)
    (path / "state.json").write_text(json.dumps(body, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def parse_args(argv: list[str]) -> dict:
    opts = {"task": "", "resume": None}
    index = 1
    while index < len(argv):
        item = argv[index]
        if item in ("-p", "--print") and index + 1 < len(argv):
            opts["task"] = argv[index + 1]
            index += 2
        elif item == "--resume" and index + 1 < len(argv):
            opts["resume"] = argv[index + 1]
            index += 2
        elif item in (
            "--output-format",
            "--permission-mode",
            "--mcp-config",
            "--append-system-prompt",
            "--max-turns",
            "--model",
        ) and index + 1 < len(argv):
            index += 2
        else:
            index += 1
    return opts


def messages() -> list[dict]:
    incoming = os.environ.get("FACTORY_IN")
    if not incoming:
        return []
    path = Path(incoming) / "messages.json"
    if not path.is_file():
        return []
    body = json.loads(path.read_text(encoding="utf-8"))
    return body if isinstance(body, list) else []


def mcp(name: str, arguments: dict) -> dict:
    tool = os.environ.get("FACTORYCTL")
    if not tool:
        raise RuntimeError("FACTORYCTL missing")
    payload = "".join(
        json.dumps(item, ensure_ascii=False) + "\n"
        for item in (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "m17-08-agent", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
    )
    completed = subprocess.run(
        [sys.executable, tool, "mcp"],
        input=payload.encode("utf-8"),
        capture_output=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", "replace")[:500])
    reply = None
    for line in completed.stdout.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("id") == 2:
            reply = item
    if reply is None:
        raise RuntimeError("mcp reply missing for " + name)
    if "error" in reply:
        raise RuntimeError(name + " " + json.dumps(reply["error"], ensure_ascii=False))
    result = reply.get("result") or {}
    if result.get("isError"):
        text = ((result.get("content") or [{}])[0]).get("text", "")
        raise RuntimeError(name + " " + text)
    structured = result.get("structuredContent") or {}
    return structured.get("result")


def factory_root() -> Path | None:
    workspace = os.environ.get("FACTORY_WORKSPACE")
    if not workspace:
        return None
    root = Path(workspace)
    for _ in range(8):
        if (root / "factory.db").is_file() and (root / "factory.json").is_file():
            return root
        if root.parent == root:
            return None
        root = root.parent
    return None


def admitted_beans() -> list[str]:
    root = factory_root()
    if root is None:
        return []
    uri = "file:" + str(root / "factory.db").replace("\\", "/") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT bean_id FROM bean_labels WHERE key = 'src' AND value = 'scout' ORDER BY at, bean_id"
        )
        return [str(row[0]) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def source_files(scope: str) -> list[tuple[str, str, int]]:
    if not scope.startswith("raw:"):
        return []
    root = Path(scope[len("raw:") :])
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8", "replace")
        found.append((path.name, digest, len(text.splitlines())))
    return found


def objective_view() -> tuple[dict, dict]:
    listed = mcp("objective_list", {})
    if not listed:
        raise RuntimeError("no objective")
    current = listed[0]
    asset = mcp("asset_get", {"asset_id": current["asset_id"]})
    return current, asset


def line_port(line_key: str) -> str:
    document = mcp("registry_get", {"key": line_key})
    inputs = ((document.get("spec") or {}).get("interface") or {}).get("inputs") or {}
    if not inputs:
        raise RuntimeError("line has no inputs")
    return sorted(inputs)[0]


def contract_cases(contract_id: str) -> list[str]:
    asset = mcp("asset_get", {"asset_id": contract_id})
    cases = ((asset.get("document") or {}).get("data") or {}).get("cases") or []
    return [str(item["name"]) for item in cases if isinstance(item, dict) and item.get("name")]


def put_asset(document: dict) -> str:
    record = mcp("asset_put", {"document": document})
    return str(record["id"])


def existing_plan(cycle_id: str) -> str | None:
    records = mcp("asset_list", {"kind": "objective_plan"})
    for record in records:
        data = (record.get("document") or {}).get("data") or {}
        if data.get("cycle_id") == cycle_id:
            return str(record["id"])
    return None


def foreman_turn(notes: dict) -> None:
    current, asset = objective_view()
    document = asset["document"]
    data = document["data"]
    scope = str(data["observation_scope"])
    grant = current["grant"]
    files = source_files(scope)
    incoming = []
    for message in messages():
        if message.get("kind") != "message":
            continue
        text = (message.get("body") or {}).get("text")
        if not isinstance(text, str) or not text.startswith("{"):
            continue
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(body, dict):
            incoming.append(body)
    for item in incoming:
        if item.get("kind") == "observed" and item.get("digest") not in {
            row["digest"] for row in notes["seen"]
        }:
            notes["seen"].append(item)
        if item.get("kind") == "result":
            if item.get("state") == "SUCCEEDED" and item.get("digest"):
                notes["done"].append(item["digest"])
            elif item.get("digest"):
                notes["failed"].append(item)
    save_notebook(notes)
    cycle_messages = [item for item in messages() if item.get("kind") == "objective_cycle"]
    if not cycle_messages:
        for item in incoming:
            if item.get("kind") == "result" and item.get("cycle_id") and item.get("job_id"):
                evaluate(str(item["cycle_id"]), data["contract"], str(item["job_id"]))
        return
    cycle_id = str(cycle_messages[-1]["body"]["cycle_id"])
    plan_id = existing_plan(cycle_id)
    if plan_id is None:
        plan_id = commit_cycle(cycle_id, current, document, grant, files, notes)
    plan = mcp("asset_get", {"asset_id": plan_id})
    plan_data = (plan.get("document") or {}).get("data") or {}
    for task in plan_data.get("tasks") or []:
        mcp("objective_dispatch", {"cycle_id": cycle_id, "task_id": str(task["id"])})
    evaluated = False
    for item in incoming:
        if item.get("kind") == "result" and item.get("cycle_id") == cycle_id and item.get("job_id"):
            evaluate(cycle_id, data["contract"], str(item["job_id"]))
            evaluated = True
    if plan_data.get("disposition") == "work" and not evaluated:
        raise RuntimeError("wait-for-result")


def commit_cycle(cycle_id: str, current: dict, document: dict, grant: dict, files: list, notes: dict) -> str:
    del document
    listing = hashlib.sha256(
        "\n".join(name + ":" + digest for name, digest, _lines in files).encode("utf-8")
    ).hexdigest()
    consumed = set(notes["done"])
    failed = {item.get("digest"): item for item in notes["failed"] if item.get("digest") not in consumed}
    pending = [item for item in notes["seen"] if item.get("digest") not in consumed]
    chosen = None
    adjustment = None
    for item in pending:
        if item.get("digest") in failed:
            chosen = item
            adjustment = failed[item["digest"]]
            break
    if chosen is None and pending:
        chosen = pending[0]
    observation = {
        "key": "observation." + cycle_id,
        "kind": "dataset",
        "summary": "本周期读取原始资料目录后的观察",
        "scope": {"subject": "factory"},
        "beans": [chosen["bean"]] if chosen else [],
        "requires": [],
        "data": {
            "listing_digest": listing,
            "records": [{"name": name, "digest": digest, "lines": lines} for name, digest, lines in files],
            "selected_digest": None if chosen is None else chosen["digest"],
        },
    }
    observation_id = put_asset(observation)
    review = int(grant["every_s"])
    if chosen is None:
        plan_data = {
            "cycle_id": cycle_id,
            "observations": [{"asset_id": observation_id, "note": "原始资料目录没有新的未完成记录"}],
            "candidates": [],
            "selected": [],
            "disposition": "idle",
            "tasks": [],
            "review_after_s": review,
            "reason": "空快照，" + str(review) + " 秒后再观察",
        }
        beans: list[str] = []
    else:
        line = str(grant["lines"][0])
        port = line_port(line)
        digest = str(chosen["digest"])
        candidate = "c-" + digest[:12]
        task_id = "t-" + digest[:12]
        reason = "来源摘要 " + digest + " 需要整理"
        if adjustment is not None:
            task_id = "t-" + digest[:8] + "-" + str(adjustment.get("run_id", "retry"))[-6:]
            reason = "根据失败运行 " + str(adjustment.get("run_id")) + " 调整后重新整理来源 " + digest
        plan_data = {
            "cycle_id": cycle_id,
            "observations": [{"asset_id": observation_id, "note": "读到未完成来源 " + digest}],
            "candidates": [{"id": candidate, "reason": reason, "evidence": [observation_id]}],
            "selected": [candidate],
            "disposition": "work",
            "tasks": [
                {
                    "id": task_id,
                    "candidate": candidate,
                    "agent": str(grant["agents"][0]),
                    "line": line,
                    "inputs": {port: chosen["bean"]},
                }
            ],
            "review_after_s": review,
            "reason": reason,
        }
        beans = [str(chosen["bean"])]
    plan = {
        "key": "plan." + cycle_id,
        "kind": "objective_plan",
        "summary": plan_data["reason"],
        "scope": {"subject": "factory"},
        "beans": beans,
        "requires": [str(current["asset_id"]), observation_id],
        "data": plan_data,
    }
    plan_id = put_asset(plan)
    mcp("objective_commit_plan", {"cycle_id": cycle_id, "plan": plan_id})
    return plan_id


def evaluate(cycle_id: str, contract_id: str, job_id: str) -> dict:
    evidence = {name: {"kind": "job", "id": job_id} for name in contract_cases(contract_id)}
    return mcp("objective_evaluate", {"cycle_id": cycle_id, "evidence": evidence})


def scout_turn(notes: dict) -> None:
    _current, asset = objective_view()
    scope = str(asset["document"]["data"]["observation_scope"])
    files = {digest: name for name, digest, _lines in source_files(scope)}
    known = {item.get("digest") for item in notes["seen"]}
    fresh = [digest for digest in files if digest not in known]
    known_beans = {item.get("bean") for item in notes["seen"]}
    beans = [bean for bean in admitted_beans() if bean not in known_beans]
    for message in messages():
        if message.get("kind") == "bean":
            bean = (message.get("body") or {}).get("bean")
            if bean and str(bean) not in beans and str(bean) not in known_beans:
                beans.append(str(bean))
    paired = list(zip(fresh, beans, strict=False))
    for digest, bean in paired:
        mcp("bean_get", {"bean_id": bean})
        text = json.dumps(
            {
                "kind": "observed",
                "bean": bean,
                "digest": digest,
                "name": files[digest],
            },
            ensure_ascii=False,
        )
        mcp("agent_send", {"to": "foreman", "text": text})
        notes["seen"].append({"kind": "observed", "bean": bean, "digest": digest, "name": files[digest]})
    if paired:
        save_notebook(notes)
    for message in messages():
        if message.get("kind") != "objective_task":
            continue
        body = message.get("body") or {}
        cycle_id = str(body.get("cycle_id") or "")
        task_id = str(body.get("task_id") or "")
        line = str(body.get("line") or "")
        inputs = body.get("inputs") or {}
        started = mcp(
            "run_start",
            {
                "line": line,
                "inputs": inputs,
                "request_key": cycle_id + "/" + task_id,
                "cycle_id": cycle_id,
                "task_id": task_id,
            },
        )
        log("started " + json.dumps(started, ensure_ascii=False))
    for message in messages():
        if message.get("kind") != "run_result":
            continue
        body = message.get("body") or {}
        run_id = str(body.get("run_id") or "")
        if not run_id:
            continue
        run = mcp("run_get", {"run_id": run_id})
        job_id = ""
        for node in run.get("nodes") or []:
            if node.get("job_id"):
                job_id = str(node["job_id"])
        digest = ""
        plan_id = ""
        for item in notes["seen"]:
            digest = str(item.get("digest") or digest)
        text = json.dumps(
            {
                "kind": "result",
                "run_id": run_id,
                "state": run.get("state"),
                "job_id": job_id,
                "cycle_id": body.get("cycle_id"),
                "task_id": body.get("task_id"),
                "digest": digest,
                "plan": plan_id,
            },
            ensure_ascii=False,
        )
        mcp("agent_send", {"to": "foreman", "text": text})
        if run.get("state") != "SUCCEEDED" and digest:
            notes["failed"].append({"digest": digest, "run_id": run_id, "job_id": job_id})
            save_notebook(notes)


def main(argv: list[str]) -> int:
    opts = parse_args(argv)
    session = opts["resume"] or "m17-08-" + os.environ.get("FACTORY_JOB_ID", "turn")
    emit({"type": "system", "subtype": "init", "session_id": session, "model": "fake-model-1", "tools": []})
    root = factory_root()
    if root is not None:
        hold = root / "hold-turn"
        while hold.is_file():
            time.sleep(0.05)
    try:
        role = os.environ.get("FACTORY_ROLE") or ""
        notes = notebook()
        if role == "foreman":
            foreman_turn(notes)
        elif os.environ.get("FACTORY_AGENT") == "scout":
            scout_turn(notes)
        cost = 0.0
        if root is not None and (root / "model-price.json").is_file():
            cost = float(json.loads((root / "model-price.json").read_text(encoding="utf-8")).get("usd", 0))
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "duration_ms": 0,
                "num_turns": 1,
                "result": "observed",
                "session_id": session,
                "total_cost_usd": cost,
            }
        )
        return 0
    except Exception as exc:
        log("agent failed: " + str(exc))
        emit(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "duration_ms": 0,
                "num_turns": 1,
                "result": str(exc),
                "session_id": session,
                "total_cost_usd": 0.0,
            }
        )
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
