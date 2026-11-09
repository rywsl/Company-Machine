#!/usr/bin/env python3
"""夹具工具 exit-three：无论参数如何，退出码都是 3。

note-dead@1 的中间工位用它证明 fail-fast。只用标准库，兼容 Python 3.8。
"""

import sys


def main(argv: list[str]) -> int:
    del argv
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
