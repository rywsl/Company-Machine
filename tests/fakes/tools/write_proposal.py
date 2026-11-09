#!/usr/bin/env python3
"""夹具工具 write-proposal：把报价方案写入 proposal.json。

quote@1 的 propose argv 用它。cost_estimate_usd 来自参数。只用标准库，兼容 Python 3.8。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    dest = Path(argv[1])
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "summary": "quote",
        "cost_estimate_usd": float(argv[0]),
        "time_estimate_s": 1,
    }
    temporary = dest.with_name(dest.name + ".tmp")
    temporary.write_text(json.dumps(body), encoding="utf-8")
    temporary.replace(dest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
