#!/usr/bin/env python3
"""夹具工具 publish-flaky：复制之后以退出码 1 结束。

publish-flaky@1 用它制造 SINK_OUTCOME_UNKNOWN。只用标准库，兼容 Python 3.8。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if argv:
        src = Path(argv[0])
        if src.is_file():
            shutil.copyfile(src, src.with_name(src.name + ".published"))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
