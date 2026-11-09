#!/usr/bin/env python3
"""夹具工具 publish-copy：把输入复制到目录，并写下 receipt.json。

publish-local@1 的 run 用它代替尚未安装的 factoryctl receipt。只用标准库，兼容 Python 3.8。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    src = Path(argv[0])
    dest_dir = Path(argv[1])
    if not src.is_file():
        return 1
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / src.name
    shutil.copyfile(src, target)
    digest = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
    receipt = {
        "target": str(dest_dir),
        "locator": {"path": str(target)},
        "items": [{"bean": "", "locator": str(target), "digest": digest}],
        "note": "",
    }
    ctl = Path(os.environ.get("FACTORY_CTL", "/factory/ctl"))
    ctl.mkdir(parents=True, exist_ok=True)
    dest = ctl / "receipt.json"
    temporary = dest.with_name(dest.name + ".tmp")
    temporary.write_text(json.dumps(receipt), encoding="utf-8")
    temporary.replace(dest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
