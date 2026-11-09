#!/usr/bin/env python3
"""测试替身：脚本化的 agent CLI（spec/system/testing.md 第 4.1 节）。

命令行模仿 Claude Code 无头模式（只解析需要的参数，其余忽略）：

    fake-agent -p TASK [--append-system-prompt TEXT] [--output-format stream-json]
               [--max-turns N] [--model M] [--verbose]

行为由一个 JSON 脚本决定。查找顺序：环境变量 FAKE_AGENT_SCRIPT 指向的文件 →
$FACTORY_IN/task/task.json（agent.task@1 输入 Bean 的 payload）。脚本格式：

    {
      "cost_usd": 0.01,                 # 结果事件里报告的成本（默认 0.0）
      "turns": 2,                       # 结果事件里报告的轮数（默认 1）
      "default": [动作...],              # attempts 中没有本次 attempt 时使用
      "attempts": {"1": [动作...], "2": [动作...]}   # 按 $FACTORY_ATTEMPT 选择
    }

动作（按顺序执行）：
    {"do": "write", "path": P, "text": T}          写文件（自动建父目录）
    {"do": "copy", "from": P, "to": Q}             复制文件
    {"do": "delete", "path": P}                    删除文件（不存在也不报错）
    {"do": "require", "path": P}                   P 必须存在，否则以错误结束
    {"do": "ctl", "args": [...], "allow_exit": [0], "save": P}
                                                   运行 factoryctl；退出码不在 allow_exit（默认 [0]）中
                                                   则以错误结束；save 给出时把 stdout 写入 P
    {"do": "sleep", "seconds": S}                  睡眠
    {"do": "cost", "usd": X}                       修改要报告的成本
    {"do": "emit", "event": {...}}                 原样输出一条 stream-json 事件
    {"do": "garbage", "text": T}                   输出一行不是 JSON 的文本
    {"do": "echo_task", "path": P}                 把收到的 -p 文本写入 P
    {"do": "exit", "code": N}                      立即结束，退出码 N（N != 0 时 is_error 为 true）

路径：以 /factory/in、/factory/out、/factory/ctl、/workspace 开头的路径，按环境变量
FACTORY_IN / FACTORY_OUT / FACTORY_CTL / FACTORY_WORKSPACE 换成真实路径（Docker 下不变，
LocalProvider 下指向宿主目录）；相对路径相对于当前目录。

stdout 是 stream-json：先是一条 system/init 事件，每个动作一条 assistant 事件，最后一条 result 事件：
    {"type": "result", "subtype": "success" | "error_during_execution", "is_error": bool,
     "duration_ms": 0, "num_turns": N, "result": "...", "session_id": "...", "total_cost_usd": X}
动作失败时输出 is_error 为 true 的 result 事件，退出码 1。
只用标准库，兼容 Python 3.8。
"""

import json
import os
import shutil
import subprocess
import sys
import time

VIRTUAL = (
    ("/factory/in", "FACTORY_IN"),
    ("/factory/out", "FACTORY_OUT"),
    ("/factory/ctl", "FACTORY_CTL"),
    ("/workspace", "FACTORY_WORKSPACE"),
)


class AgentError(Exception):
    pass


def real_path(path):
    for prefix, var in VIRTUAL:
        if path == prefix or path.startswith(prefix + "/"):
            base = os.environ.get(var)
            if base:
                return base + path[len(prefix) :]
    return path


def parse_args(argv):
    opts = {"task": "", "model": "", "max_turns": None, "system": ""}
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("-p", "--print") and i + 1 < len(argv):
            opts["task"] = argv[i + 1]
            i += 2
        elif a == "--model" and i + 1 < len(argv):
            opts["model"] = argv[i + 1]
            i += 2
        elif a == "--max-turns" and i + 1 < len(argv):
            opts["max_turns"] = argv[i + 1]
            i += 2
        elif a == "--append-system-prompt" and i + 1 < len(argv):
            opts["system"] = argv[i + 1]
            i += 2
        elif a in ("--output-format", "--permission-mode", "--mcp-config") and i + 1 < len(argv):
            i += 2
        else:
            i += 1
    return opts


def load_script():
    path = os.environ.get("FAKE_AGENT_SCRIPT")
    if not path:
        path = os.path.join(os.environ.get("FACTORY_IN", "/factory/in"), "task", "task.json")
    with open(path, encoding="utf-8") as f:
        script = json.load(f)
    if not isinstance(script, dict):
        raise AgentError("脚本必须是 JSON 对象")
    return script


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


class Agent:
    def __init__(self, opts, script, session):
        self.opts = opts
        self.script = script
        self.session = session
        self.cost = float(script.get("cost_usd", 0.0))
        self.turns = int(script.get("turns", 1))

    def say(self, text):
        emit(
            {
                "type": "assistant",
                "session_id": self.session,
                "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            }
        )

    def finish(self, ok, text):
        emit(
            {
                "type": "result",
                "subtype": "success" if ok else "error_during_execution",
                "is_error": not ok,
                "duration_ms": 0,
                "num_turns": self.turns,
                "result": text,
                "session_id": self.session,
                "total_cost_usd": self.cost,
            }
        )

    def run(self, actions):
        """执行动作；返回退出码（遇到 exit 动作时提前返回）。"""
        for action in actions:
            if not isinstance(action, dict) or "do" not in action:
                raise AgentError(f"动作格式不对：{action!r}")
            code = self.step(action)
            if code is not None:
                return code
        return 0

    def step(self, a):
        do = a["do"]
        self.say(f"执行动作 {do}")
        if do == "write":
            path = real_path(a["path"])
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(a.get("text", ""))
        elif do == "copy":
            dst = real_path(a["to"])
            parent = os.path.dirname(dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.copyfile(real_path(a["from"]), dst)
        elif do == "delete":
            path = real_path(a["path"])
            if os.path.isfile(path):
                os.remove(path)
        elif do == "require":
            if not os.path.exists(real_path(a["path"])):
                raise AgentError("需要的文件不存在：{}".format(a["path"]))
        elif do == "ctl":
            self.ctl(a)
        elif do == "sleep":
            time.sleep(float(a["seconds"]))
        elif do == "cost":
            self.cost = float(a["usd"])
        elif do == "emit":
            emit(a["event"])
        elif do == "garbage":
            sys.stdout.write(str(a.get("text", "not json")) + "\n")
            sys.stdout.flush()
        elif do == "echo_task":
            path = real_path(a["path"])
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.opts["task"])
        elif do == "exit":
            return int(a.get("code", 0))
        else:
            raise AgentError(f"未知动作 {do}")
        return None

    def ctl(self, a):
        tool = os.environ.get("FACTORYCTL")
        if not tool:
            raise AgentError("环境变量 FACTORYCTL 没有设置")
        args = [str(x) for x in a.get("args", [])]
        r = subprocess.run(
            [sys.executable, tool] + args,
            capture_output=True,
            timeout=float(a.get("timeout", 600)),
        )
        out = r.stdout.decode("utf-8", "replace")
        if a.get("save"):
            path = real_path(a["save"])
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(out)
        allowed = a.get("allow_exit", [0])
        if r.returncode not in allowed:
            err = r.stderr.decode("utf-8", "replace").strip()
            raise AgentError("factoryctl {} 退出码 {}：{}".format(" ".join(args), r.returncode, err))


def main(argv):
    opts = parse_args(argv)
    attempt = os.environ.get("FACTORY_ATTEMPT", "1")
    session = "fake-{}-{}".format(os.environ.get("FACTORY_JOB_ID", "job"), attempt)
    emit({"type": "system", "subtype": "init", "session_id": session, "model": opts["model"], "tools": []})
    try:
        script = load_script()
    except (OSError, ValueError, AgentError) as e:
        Agent(opts, {}, session).finish(False, f"无法读取脚本：{e}")
        return 1
    agent = Agent(opts, script, session)
    attempts = script.get("attempts") or {}
    actions = attempts.get(attempt, script.get("default", []))
    try:
        code = agent.run(actions)
    except (AgentError, OSError, KeyError, ValueError, subprocess.TimeoutExpired) as e:
        agent.finish(False, str(e))
        return 1
    agent.finish(code == 0, "完成" if code == 0 else f"退出码 {code}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
