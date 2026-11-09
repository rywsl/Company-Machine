#!/usr/bin/env python3
"""夹具工具 upper-text IN OUT：把文本转成大写（spec/system/testing.md 第 4.2 节）。

行为由输入内容决定，这样 Docker 与 Local 下完全一致：
- 输入中有一行恰好是 `#fail`：写一行错误信息到 stderr，退出码 3，不写输出；
- 输入中有一行恰好是 `#slow`：先睡 30 秒再继续（用于超时测试）；
- 输入中有一行恰好是 `#keep-lower`：原样复制，不转大写（用于制造不满足 shouting 的产物）。
只用标准库，兼容 Python 3.8。
"""

import os
import sys
import time


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("用法：upper-text IN OUT\n")
        return 2
    src, dst = argv[1], argv[2]
    with open(src, encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines()
    if "#fail" in lines:
        sys.stderr.write("upper-text: 输入要求失败（#fail）\n")
        return 3
    if "#slow" in lines:
        time.sleep(30)
    out = text if "#keep-lower" in lines else text.upper()
    parent = os.path.dirname(dst)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
