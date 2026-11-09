#!/usr/bin/env python3
"""夹具 validator count-text PATH：note.text@1 的实测（spec/system/testing.md 第 4.2 节）。

- 接受：退出码 0，stdout 是一个 JSON 对象，直接给出 facts：
  {"chars": 字符数, "lines": 行数, "has_lower": 是否含小写字母}
- 拒绝：退出码 1，stdout 是 {"reason": "..."}。文件不是合法 UTF-8，或去掉空白后为空，都拒绝。
- 用法错误或文件不存在：退出码 2（检验设施故障，不是产物的问题）。
只用标准库，兼容 Python 3.8。
"""

import json
import sys


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("用法：count-text PATH\n")
        return 2
    try:
        with open(argv[1], "rb") as f:
            data = f.read()
    except OSError as e:
        sys.stderr.write(f"count-text: 无法读取 {argv[1]}：{e}\n")
        return 2
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        print(json.dumps({"reason": "不是合法的 UTF-8 文本"}, ensure_ascii=False))
        return 1
    if not text.strip():
        print(json.dumps({"reason": "文本为空"}, ensure_ascii=False))
        return 1
    facts = {
        "chars": len(text),
        "lines": len(text.splitlines()),
        "has_lower": any(c.islower() for c in text),
    }
    print(json.dumps(facts, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
