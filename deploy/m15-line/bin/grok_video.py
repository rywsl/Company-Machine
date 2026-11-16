"""用 Grok 按参考视频改一支 5 秒试片，再把原声合成进去。

接口是 createsis 上的 xAI 视频编辑：POST /v1/videos/edits，模型
grok-imagine-video。源视频最长 8.7 秒，输出沿用源视频的时长。这里先裁成 5 秒。
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

ORIGIN = os.environ.get("GROK_BASE", "https://openai.createsis.cn").rstrip("/")
MODEL = "grok-imagine-video"


def main() -> int:
    try:
        _run()
    except Exception as exc:  # noqa: BLE001 — 脚本工位用退出码和 stderr 报告失败
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    return 0


def _run() -> None:
    root_in = Path(os.environ.get("FACTORY_IN", "/factory/in"))
    motion = _first(root_in, (".mp4",))
    if motion is None:
        raise RuntimeError("找不到参考视频")
    token = os.environ.get("GROK_API_KEY", "")
    if not token:
        raise RuntimeError("没有 GROK_API_KEY")
    brief = _brief(root_in / "brief" / "brief.json")
    prompt = os.environ.get("DANCE_PROMPT") or str(brief.get("motion") or brief.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("没有动作提示")
    clip = _five_seconds(motion)
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "video": {"url": "data:video/mp4;base64," + base64.b64encode(clip.read_bytes()).decode("ascii")},
    }
    created = _json("POST", ORIGIN + "/v1/videos/edits", payload, token)
    request_id = _request_id(created)
    print(f"request_id={request_id}", file=sys.stderr, flush=True)
    deadline = time.monotonic() + 540
    while True:
        found = _json("GET", f"{ORIGIN}/v1/videos/{request_id}", None, token)
        status = str(found.get("status") or "").lower()
        print(f"status={status or 'unknown'}", file=sys.stderr, flush=True)
        if status in {"done", "succeeded", "success", "completed"}:
            url = _video_url(found)
            dest = Path(os.environ.get("FACTORY_OUT", "/factory/out")) / "video" / "video.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            raw = _bytes(url, token)
            audio = _first(root_in, (".mp3", ".wav", ".flac"))
            if audio is None:
                dest.write_bytes(raw)
            else:
                _mux(raw, audio, dest)
            return
        if status in {"failed", "error", "expired", "canceled", "cancelled"}:
            raise RuntimeError("视频编辑失败：" + json.dumps(found, ensure_ascii=False)[:500])
        if time.monotonic() > deadline:
            raise RuntimeError(f"视频编辑仍未完成：{request_id}")
        time.sleep(5)


def _brief(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    body = json.loads(path.read_text(encoding="utf-8"))
    return body if isinstance(body, dict) else {}


def _first(folder: Path, suffixes: tuple[str, ...]) -> Path | None:
    if not folder.is_dir():
        return None
    files = [
        path
        for path in sorted(folder.rglob("*"))
        if path.is_file() and path.suffix.lower() in suffixes and path.name != "video.src.mp4"
    ]
    return files[0] if files else None


def _five_seconds(path: Path) -> Path:
    dest = Path(os.environ.get("FACTORY_TMP", "/tmp")) / "motion-5s.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(path),
            "-t",
            "5",
            "-an",
            "-vf",
            "scale=-2:480",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise RuntimeError("参考视频裁成 5 秒失败：" + proc.stderr.decode("utf-8", errors="replace")[-300:])
    print(f"motion_bytes={dest.stat().st_size}", file=sys.stderr, flush=True)
    return dest


def _request_id(body: dict[str, object]) -> str:
    for key in ("request_id", "id", "task_id"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    data = body.get("data")
    if isinstance(data, dict):
        return _request_id(data)
    raise RuntimeError("提交没有返回 request_id：" + json.dumps(body, ensure_ascii=False)[:300])


def _video_url(body: dict[str, object]) -> str:
    found = _url_of(body)
    if found is None:
        data = body.get("data")
        if isinstance(data, dict):
            found = _url_of(data)
    if found is None:
        raise RuntimeError("成功结果里没有视频地址：" + json.dumps(body, ensure_ascii=False)[:300])
    if found.startswith("/"):
        return ORIGIN + found
    return found


def _url_of(body: dict[str, object]) -> str | None:
    video = body.get("video")
    if isinstance(video, dict) and isinstance(video.get("url"), str):
        return str(video["url"])
    for key in ("url", "video_url"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _json(method: str, url: str, payload: dict[str, object] | None, token: str) -> dict[str, object]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    if not isinstance(body, dict):
        raise RuntimeError("响应不是 JSON 对象")
    error = body.get("error")
    if isinstance(error, dict) and error.get("message"):
        raise RuntimeError(str(error.get("message"))[:400])
    return body


def _bytes(url: str, token: str) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token}, method="GET")
    with urllib.request.urlopen(req, timeout=180) as resp:
        payload = resp.read()
    if not payload:
        raise RuntimeError("视频是空的")
    return payload


def _mux(video: bytes, audio: Path, dest: Path) -> None:
    raw = dest.parent / "video.src.mp4"
    raw.write_bytes(video)
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(raw),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-t",
            "5",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            str(dest),
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise RuntimeError("原声合成失败：" + proc.stderr.decode("utf-8", errors="replace")[-300:])
    raw.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
