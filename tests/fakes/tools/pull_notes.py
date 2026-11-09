#!/usr/bin/env python3
"""夹具工具 pull-notes：从 from_dir 读取第一份文本，写入输出路径。

只用标准库，兼容 Python 3.8。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    src_dir = Path(argv[0])
    dest = Path(argv[1])
    files = sorted(path for path in src_dir.glob("*.txt") if path.is_file())
    if not files:
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(files[0], dest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
