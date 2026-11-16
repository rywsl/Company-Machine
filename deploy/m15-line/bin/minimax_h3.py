"""把静帧交给 AutoDL MiniMax H3（minimax_h3_zm_u24），收下 MP4。

参考图用 data URI，接口见
https://www.autodl.art/large-model/comfyui/minimax_h3_zm_u24
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
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
    image_path = _first(root_in, (".png", ".jpg", ".jpeg", ".webp"))
    if image_path is None:
        raise RuntimeError("找不到参考图")
    brief = _brief(root_in / "brief" / "brief.json")
    prompt = os.environ.get("DANCE_PROMPT") or str(brief.get("motion") or brief.get("prompt") or "").strip()
    if not prompt:
        prompt = "the subject shifts slightly, camera holds still"
    duration = _duration(brief.get("duration"))
    resolution = str(brief.get("resolution") or "768p竖")
    token = os.environ.get("AUTODL_AK", "")
    if not token:
        raise RuntimeError("没有 AUTODL_AK")
    payload: dict[str, object] = {
        "prompt": prompt,
        "duration": duration,
        "resolution": resolution,
    }
    for index, uri in enumerate(_image_uris(image_path)):
        payload[f"ref_image_{index}"] = uri
    audio = _first(root_in, (".mp3", ".wav", ".flac"))
    if audio is not None:
        kind = "mpeg" if audio.suffix.lower() == ".mp3" else audio.suffix.lower().lstrip(".")
        payload["ref_audio_0"] = f"data:audio/{kind};base64," + base64.b64encode(audio.read_bytes()).decode("ascii")
    created = _submit(payload, token)
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
            raw = _bytes(url)
            if audio is None:
                dest.write_bytes(raw)
            else:
                _mux(raw, audio, dest)
            return
        if status in {"FAILED", "ERROR", "CANCELED", "CANCELLED"}:
            raise RuntimeError("视频任务失败：" + json.dumps(current, ensure_ascii=False)[:500])
        if time.monotonic() > deadline:
            raise RuntimeError(f"视频任务仍未完成：{task_id}")
        time.sleep(5)


def _brief(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    body = json.loads(path.read_text(encoding="utf-8"))
    return body if isinstance(body, dict) else {}


def _duration(value: object) -> int:
    try:
        number = int(value) if value is not None else 15
    except (TypeError, ValueError):
        number = 15
    if number < 1:
        return 1
    if number > 15:
        print(f"duration {number} 超过接口上限，按 15 秒提交", file=sys.stderr)
        return 15
    return number


def _first(folder: Path, suffixes: tuple[str, ...]) -> Path | None:
    if not folder.is_dir():
        return None
    files = [
        path
        for path in sorted(folder.rglob("*"))
        if path.is_file() and path.suffix.lower() in suffixes
    ]
    return files[0] if files else None


def _image_uris(path: Path) -> list[str]:
    crops = _crops(path)
    uris = []
    for item in crops:
        uris.append("data:image/jpeg;base64," + base64.b64encode(item).decode("ascii"))
    return uris


def _crops(path: Path) -> list[bytes]:
    probed = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    parts = (probed.stdout or "").strip().split(",")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return [path.read_bytes()]
    width, height = int(parts[0]), int(parts[1])
    boxes = [(0, 0, width, height)]
    if width > int(height * 1.3):
        top = max(1, int(height * 0.68))
        third = max(1, width // 3)
        boxes = [(8, 8, third - 16, top), (third, 8, third, top), (third * 2, 8, width - third * 2 - 8, top)]
    found = []
    for index, (x, y, w, h) in enumerate(boxes):
        dest = Path(os.environ.get("FACTORY_TMP", "/tmp")) / f"ref-{index}.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-vf", f"crop={w}:{h}:{x}:{y},scale=512:-1", "-frames:v", "1", "-q:v", "4", str(dest)],
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0 and dest.is_file():
            found.append(dest.read_bytes())
    if found:
        return found
    whole = Path(os.environ.get("FACTORY_TMP", "/tmp")) / "ref-whole.jpg"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(path), "-vf", "scale=512:-1", "-frames:v", "1", "-q:v", "4", str(whole)],
        capture_output=True,
        check=False,
    )
    if proc.returncode == 0 and whole.is_file():
        return [whole.read_bytes()]
    return [path.read_bytes()]


def _submit(payload: dict[str, object], token: str) -> dict[str, object]:
    try:
        return _json("POST", f"{ORIGIN}/{WORKFLOW}", payload, token)
    except RuntimeError as exc:
        if "duration" not in str(exc) or payload.get("duration") == 15:
            raise
        print("时长被拒绝，改用 15 秒重试", file=sys.stderr)
        payload = dict(payload)
        payload["duration"] = 15
        return _json("POST", f"{ORIGIN}/{WORKFLOW}", payload, token)


def _mux(video: bytes, audio: Path, dest: Path) -> None:
    folder = dest.parent
    raw = folder / "video.src.mp4"
    raw.write_bytes(video)
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(raw), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-shortest", "-c:v", "copy", "-c:a", "aac", str(dest)],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise RuntimeError("原声合成失败：" + proc.stderr.decode("utf-8", errors="replace")[-300:])
    raw.unlink(missing_ok=True)


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
