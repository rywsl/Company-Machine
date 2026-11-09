#!/usr/bin/env python3
"""夹具工具 local-file-check：回执指向的文件必须存在，且摘要一致。

只用标准库，兼容 Python 3.8。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    del argv
    receipt_path = Path(os.environ.get("FACTORY_CTL", "/factory/ctl")) / "receipt.json"
    if not receipt_path.is_file():
        return 1
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    locator = receipt.get("locator") or {}
    path = Path(str(locator.get("path", "")))
    if not path.is_file():
        return 1
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    items = receipt.get("items") or []
    if items and items[0].get("digest") and items[0]["digest"] != digest:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
