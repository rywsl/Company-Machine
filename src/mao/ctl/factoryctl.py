#!/usr/bin/env python3
"""沙箱内控制通道客户端。只用标准库。"""

import argparse
import glob
import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone


def _die(code, message):
    sys.stderr.write(message + "\n")
    raise SystemExit(code)


def _ctl_dir():
    raw = os.environ.get("FACTORY_CTL") or ""
    if not raw or not os.path.isdir(raw):
        _die(4, "请设置 FACTORY_CTL 指向控制通道目录")
    return raw


def _real(path):
    prefixes = (
        ("/factory/in", "FACTORY_IN"),
        ("/factory/out", "FACTORY_OUT"),
        ("/factory/ctl", "FACTORY_CTL"),
        ("/workspace", "FACTORY_WORKSPACE"),
    )
    for prefix, name in prefixes:
        value = os.environ.get(name) or ""
        if not value:
            continue
        if path == prefix or path.startswith(prefix + "/"):
            rest = path[len(prefix) :]
            return value + rest.replace("/", os.sep)
    return path


def _dump(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _atomic(path, text):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, path)


def _read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _job():
    path = os.path.join(_ctl_dir(), "job.json")
    if not os.path.isfile(path):
        _die(4, "job.json 不存在")
    job = _read_json(path)
    for key in ("job_id", "attempt", "inputs"):
        if key not in job:
            _die(4, "job.json 缺字段")
    return job


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017


def cmd_inputs(args):
    job = _job()
    if args.json:
        sys.stdout.write(
            _dump({"job_id": job["job_id"], "attempt": job["attempt"], "inputs": job["inputs"]}) + "\n"
        )
        return
    inputs = job["inputs"]
    for port in sorted(inputs):
        value = inputs[port]
        if isinstance(value, list):
            for index, item in enumerate(value):
                sys.stdout.write(
                    "{}\t[{}]{}\t{}\n".format(port, index, item.get("id", ""), item.get("type", ""))
                )
        else:
            sys.stdout.write("{}\t{}\t{}\n".format(port, value.get("id", ""), value.get("type", "")))


def cmd_contract(_args):
    path = os.path.join(_ctl_dir(), "contract.json")
    if not os.path.isfile(path):
        _die(4, "contract.json 不存在")
    with open(path, encoding="utf-8") as handle:
        data = handle.read()
    sys.stdout.write(data if data.endswith("\n") else data + "\n")


def cmd_budget(args):
    job = _job()
    if not args.json:
        _die(2, "budget 需要 --json")
    sys.stdout.write(
        _dump(
            {
                "budget_usd": job.get("budget_usd", 0),
                "spent_usd": None,
                "note": "M1 在 job 结束后从 transcript 结算，运行中没有余额",
            }
        )
        + "\n"
    )


def cmd_submit(args):
    meta = {}
    for item in args.meta or []:
        if "=" not in item:
            _die(2, "--meta 必须是 KEY=VALUE")
        key, value = item.split("=", 1)
        if not key:
            _die(2, "--meta 必须是 KEY=VALUE")
        meta[key] = value
    source = _real(args.path)
    if not os.path.isfile(source):
        _die(1, "提交的文件不存在")
    out_root = _real("/factory/out")
    dest_dir = os.path.join(out_root, args.port)
    if not os.path.isdir(dest_dir):
        os.makedirs(dest_dir)
    name = os.path.basename(source)
    shutil.copyfile(source, os.path.join(dest_dir, name))
    if meta:
        _atomic(os.path.join(dest_dir, "meta.json"), _dump(meta))
    body = {
        "port": args.port,
        "source": args.path,
        "files": [name],
        "meta": meta,
        "at": _utc_now(),
    }
    _atomic(os.path.join(_ctl_dir(), "submissions", args.port + ".json"), _dump(body))
    if args.json:
        sys.stdout.write(_dump(body) + "\n")
    else:
        sys.stdout.write(f"submitted {args.port} {name}\n")


def cmd_log(args):
    path = os.path.join(_ctl_dir(), "log.jsonl")
    line = _dump({"at": _utc_now(), "message": args.message}) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)


def cmd_mcp():
    job_path = os.path.join(_ctl_dir(), "job.json")
    if not os.path.isfile(job_path):
        _die(4, "缺少 job.json")
    job = _read_json(job_path)
    try:
        timeout_s = float(job.get("timeout_s", 60))
    except (TypeError, ValueError):
        timeout_s = 60.0
    for raw in sys.stdin:
        line = raw.strip()
        if line == "":
            continue
        try:
            message = json.loads(line)
        except ValueError:
            sys.stdout.write(
                _dump({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                + "\n"
            )
            sys.stdout.flush()
            continue
        rid = "r-" + uuid.uuid4().hex[:8]
        body = {"id": rid, "op": "mcp", "args": {"message": message}}
        _atomic(os.path.join(_ctl_dir(), "requests", rid + ".json"), _dump(body))
        response = os.path.join(_ctl_dir(), "responses", rid + ".json")
        deadline = time.time() + timeout_s
        found = None
        while time.time() < deadline:
            if os.path.isfile(response):
                found = _read_json(response)
                break
            time.sleep(0.05)
        request_id = message.get("id") if isinstance(message, dict) else None
        if found is None:
            if isinstance(message, dict) and "id" in message:
                sys.stdout.write(
                    _dump(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {"code": -32603, "message": "timeout"},
                        }
                    )
                    + "\n"
                )
                sys.stdout.flush()
            continue
        if found.get("ok") and (found.get("result") or {}).get("message") is not None:
            sys.stdout.write(_dump(found["result"]["message"]) + "\n")
            sys.stdout.flush()
        elif not found.get("ok") and isinstance(message, dict) and "id" in message:
            error = found.get("error") or {}
            sys.stdout.write(
                _dump(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32603, "message": str(error.get("code") or "error")},
                    }
                )
                + "\n"
            )
            sys.stdout.flush()


def cmd_fail(args):
    _atomic(os.path.join(_ctl_dir(), "fail.json"), _dump({"reason": args.reason}))
    raise SystemExit(1)


def _timeout_s():
    path = os.path.join(_ctl_dir(), "contract.json")
    if not os.path.isfile(path):
        return 605.0
    data = _read_json(path)
    approval = data.get("approval") or {}
    try:
        return float(approval.get("timeout_s", 600)) + 5.0
    except (TypeError, ValueError):
        return 605.0


def _wait(rid):
    response = os.path.join(_ctl_dir(), "responses", rid + ".json")
    deadline = time.time() + _timeout_s()
    while time.time() < deadline:
        if os.path.isfile(response):
            body = _read_json(response)
            if body.get("ok"):
                sys.stdout.write(_dump(body.get("result")) + "\n")
                return
            error = body.get("error") or {}
            _die(1, str(error.get("message") or "宿主拒绝了请求"))
        time.sleep(0.2)
    _die(1, "等待宿主响应超时")


def _request(op, args):
    rid = "r-" + uuid.uuid4().hex[:8]
    body = {"id": rid, "op": op, "args": args}
    _atomic(os.path.join(_ctl_dir(), "requests", rid + ".json"), _dump(body))
    _wait(rid)


def cmd_check(args):
    _request("check", {"port": args.port, "path": args.path})


def cmd_approval(args):
    if args.approval_cmd != "request":
        _die(2, "未知的 approval 子命令")
    try:
        cost = float(args.cost_usd)
    except (TypeError, ValueError):
        _die(2, "--cost-usd 必须是数字")
    if cost < 0:
        _die(2, "--cost-usd 必须是非负数字")
    _request("approval", {"kind": args.kind, "summary": args.summary, "cost_usd": cost})


def _harvest_source(workspace, rel):
    text = rel.replace("\\", "/")
    if os.path.isabs(text) or (len(text) > 2 and text[1] == ":"):
        return text.replace("/", os.sep)
    return os.path.join(workspace, text.replace("/", os.sep))


def _harvest_copy(src, dest):
    if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dest)):
        return
    parent = os.path.dirname(dest)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    shutil.copyfile(src, dest)


def _matches(pattern):
    workspace = os.environ.get("FACTORY_WORKSPACE") or ""
    if pattern.startswith("/"):
        root = _real(pattern)
        # glob the mapped path, then express matches relative to workspace when possible
        found = sorted(glob.glob(root))
        rels = []
        for item in found:
            if workspace and item.startswith(workspace):
                rels.append(item[len(workspace) :].lstrip("\\/").replace("\\", "/"))
            else:
                rels.append(item.replace("\\", "/"))
        return rels
    base = workspace or _real("/workspace")
    found = []
    for item in PathGlob(base, pattern):
        found.append(item)
    found.sort()
    return found


class PathGlob:
    def __init__(self, base, pattern):
        self.base = base
        self.pattern = pattern

    def __iter__(self):
        from pathlib import Path

        root = Path(self.base)
        for path in root.glob(self.pattern):
            if path.is_file():
                rel = path.relative_to(root).as_posix()
                yield rel


def cmd_harvest(_args):
    path = os.path.join(_ctl_dir(), "harvest.json")
    if not os.path.isfile(path):
        _die(2, "harvest.json 不存在")
    data = _read_json(path)
    if not isinstance(data, dict):
        _die(2, "harvest.json 不是对象")
    outputs = data.get("outputs") or {}
    ports = {}
    top_ok = True
    for port in sorted(outputs):
        if os.path.isfile(os.path.join(_ctl_dir(), "submissions", port + ".json")):
            continue
        spec = outputs[port]
        try:
            matches = _matches(spec.get("harvest") or "")
        except OSError:
            ports[port] = {"ok": False, "code": "HARVEST_IO", "files": [], "matches": []}
            top_ok = False
            continue
        optional = bool(spec.get("optional"))
        listed = bool(spec.get("list"))
        place = spec.get("place_as") or "file"
        if not matches and optional:
            ports[port] = {"ok": True, "files": [], "matches": []}
            continue
        if not matches:
            ports[port] = {"ok": False, "code": "HARVEST_EMPTY", "files": [], "matches": []}
            top_ok = False
            continue
        if not listed and len(matches) > 1:
            ports[port] = {"ok": False, "code": "HARVEST_AMBIGUOUS", "files": [], "matches": matches}
            top_ok = False
            continue
        files = []
        try:
            workspace = os.environ.get("FACTORY_WORKSPACE") or _real("/workspace")
            out_root = _real("/factory/out")
            if listed:
                for index, rel in enumerate(matches):
                    src = _harvest_source(workspace, rel)
                    dest = os.path.join(out_root, port, str(index), place)
                    _harvest_copy(src, dest)
                    files.append(f"{index}/{place}")
            else:
                src = _harvest_source(workspace, matches[0])
                dest = os.path.join(out_root, port, place)
                _harvest_copy(src, dest)
                files.append(place)
        except OSError:
            ports[port] = {"ok": False, "code": "HARVEST_IO", "files": [], "matches": matches}
            top_ok = False
            continue
        ports[port] = {"ok": True, "files": files, "matches": matches}
    body = {"ok": top_ok, "ports": ports}
    try:
        _atomic(os.path.join(_ctl_dir(), "harvest-result.json"), _dump(body))
    except OSError:
        _die(1, "无法写入 harvest-result.json")


def main(argv):
    _ctl_dir()
    parser = argparse.ArgumentParser(prog="factoryctl")
    sub = parser.add_subparsers(dest="cmd")
    inputs = sub.add_parser("inputs")
    inputs.add_argument("--json", action="store_true")
    contract = sub.add_parser("contract")
    contract.add_argument("--json", action="store_true")
    check = sub.add_parser("check")
    check.add_argument("port")
    check.add_argument("path")
    submit = sub.add_parser("submit")
    submit.add_argument("port")
    submit.add_argument("path")
    submit.add_argument("--meta", action="append")
    submit.add_argument("--json", action="store_true")
    approval = sub.add_parser("approval")
    approval.add_argument("approval_cmd")
    approval.add_argument("--kind", required=True)
    approval.add_argument("--summary", required=True)
    approval.add_argument("--cost-usd", default="0")
    budget = sub.add_parser("budget")
    budget.add_argument("--json", action="store_true")
    log = sub.add_parser("log")
    log.add_argument("message")
    fail = sub.add_parser("fail")
    fail.add_argument("reason")
    sub.add_parser("harvest")
    sub.add_parser("mcp")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        raise
    if args.cmd == "inputs":
        cmd_inputs(args)
    elif args.cmd == "contract":
        cmd_contract(args)
    elif args.cmd == "budget":
        cmd_budget(args)
    elif args.cmd == "submit":
        cmd_submit(args)
    elif args.cmd == "log":
        cmd_log(args)
    elif args.cmd == "fail":
        cmd_fail(args)
    elif args.cmd == "harvest":
        cmd_harvest(args)
    elif args.cmd == "check":
        cmd_check(args)
    elif args.cmd == "approval":
        cmd_approval(args)
    elif args.cmd == "mcp":
        cmd_mcp()
    else:
        _die(2, "未知子命令")


if __name__ == "__main__":
    main(sys.argv[1:])
