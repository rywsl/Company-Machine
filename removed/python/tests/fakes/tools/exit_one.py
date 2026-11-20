#!/usr/bin/env python3
"""夹具工具 exit-one：复制尚未发生时直接退出码 1。

publish-flaky@1 用它制造 SINK_OUTCOME_UNKNOWN。只用标准库，兼容 Python 3.8。
"""

import sys


def main(argv: list[str]) -> int:
    del argv
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
