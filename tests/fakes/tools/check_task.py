#!/usr/bin/env python3
"""夹具 validator check-task PATH：agent.task@1 的实测（spec/system/testing.md 第 4.2 节）。

- 接受：退出码 0，stdout 是 {"steps": 动作总数}（default 与 attempts 中全部动作的个数）。
- 拒绝：退出码 1，stdout 是 {"reason": "..."}。不是 JSON、不是对象、动作列表格式不对，都拒绝。
- 用法错误或文件不存在：退出码 2。
只用标准库，兼容 Python 3.8。
"""

import json
import sys


def count_steps(script):
    if not isinstance(script, dict):
        raise ValueError("脚本必须是 JSON 对象")
    total = 0
    default = script.get("default", [])
    if not isinstance(default, list):
        raise ValueError("default 必须是列表")
    total += len(default)
    attempts = script.get("attempts", {})
    if not isinstance(attempts, dict):
        raise ValueError("attempts 必须是对象")
    for key, actions in attempts.items():
        if not key.isdigit() or not isinstance(actions, list):
            raise ValueError("attempts 的键必须是数字字符串，值必须是列表")
        total += len(actions)
    return total


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("用法：check-task PATH\n")
        return 2
    try:
        with open(argv[1], "rb") as f:
            data = f.read()
    except OSError as e:
        sys.stderr.write(f"check-task: 无法读取 {argv[1]}：{e}\n")
        return 2
    try:
        steps = count_steps(json.loads(data.decode("utf-8")))
    except (UnicodeDecodeError, ValueError) as e:
        print(json.dumps({"reason": str(e)}, ensure_ascii=False))
        return 1
    print(json.dumps({"steps": steps}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
