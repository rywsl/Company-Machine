"""把静帧交给 AutoDL MiniMax H3（minimax_h3_zm_u24），收下 MP4。

参考图用 data URI，接口见
https://www.autodl.art/large-model/comfyui/minimax_h3_zm_u24
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

WORKFLOW = "minimax_h3_zm_u24"
ORIGIN = "https://autodl.art/api/v1/comfyui/comfyui_workflow"


def main() -> int:
    try:
        _run()
    except Exception as exc:  # noqa: BLE001 — 脚本工位用退出码和 stderr 报告失败
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _run() -> None:
    root_in = Path(os.environ.get("FACTORY_IN", "/factory/in"))
    image_path = root_in / "image" / "image.png"
    brief_path = root_in / "brief" / "brief.json"
    if not image_path.is_file():
        raise RuntimeError(f"找不到参考图 {image_path}")
    prompt = "the subject shifts slightly, camera holds still"
    if brief_path.is_file():
        body = json.loads(brief_path.read_text(encoding="utf-8"))
        if isinstance(body, dict) and str(body.get("motion") or "").strip():
            prompt = str(body["motion"]).strip()
    token = os.environ.get("AUTODL_AK", "")
    if not token:
        raise RuntimeError("没有 AUTODL_AK")
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    payload = {
        "prompt": prompt,
        "duration": 1,
        "resolution": "480p竖",
        "ref_image_0": "data:image/png;base64," + encoded,
    }
    created = _json("POST", f"{ORIGIN}/{WORKFLOW}", payload, token)
    data = _data(created)
    task_id = str(data.get("task_id") or "")
    if not task_id:
        raise RuntimeError("提交没有返回 task_id")
    print(f"task_id={task_id}", file=sys.stderr)
    deadline = time.monotonic() + 840
    while True:
        found = _json("GET", f"{ORIGIN}/result/{task_id}", None, token)
        current = _data(found)
        status = str(current.get("status") or "")
        print(f"status={status}", file=sys.stderr)
        if status == "SUCCESS":
            url = _result_url(current.get("results"))
            dest = Path(os.environ.get("FACTORY_OUT", "/factory/out")) / "video" / "video.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(_bytes(url))
            return
        if status in {"FAILED", "ERROR", "CANCELED", "CANCELLED"}:
            raise RuntimeError("视频任务失败：" + json.dumps(current, ensure_ascii=False)[:500])
        if time.monotonic() > deadline:
            raise RuntimeError(f"视频任务仍未完成：{task_id}")
        time.sleep(5)


def _data(body: dict[str, object]) -> dict[str, object]:
    data = body.get("data")
    if isinstance(data, dict):
        return data
    raise RuntimeError("响应没有 data：" + json.dumps(body, ensure_ascii=False)[:300])


def _result_url(results: object) -> str:
    if isinstance(results, list):
        for item in results:
            if isinstance(item, str) and item.startswith("http"):
                return item
            if isinstance(item, dict):
                for key in ("url", "video", "file"):
                    value = item.get(key)
                    if isinstance(value, str) and value.startswith("http"):
                        return value
    raise RuntimeError("成功结果里没有视频地址")


def _json(method: str, url: str, payload: dict[str, object] | None, token: str) -> dict[str, object]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": token, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    if not isinstance(body, dict):
        raise RuntimeError("响应不是 JSON 对象")
    return body


def _bytes(url: str) -> bytes:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = resp.read()
    if not payload:
        raise RuntimeError("视频是空的")
    return payload


if __name__ == "__main__":
    sys.exit(main())
