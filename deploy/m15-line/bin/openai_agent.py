"""gpt-6.1-sol 操作工。stdout 只写 stream-json。

生图工位：先让模型把 brief 收成一条提示词，再按 OpenMontage openai_image
的 images.generate 契约，把图交给 tbtk.asia。厂长会话只做一轮对话。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

IMAGE_URL = "https://tbtk.asia/v1/images/generations"
IMAGE_MODEL = "gpt-image-2.5"


def main() -> int:
    model = _flag("--model") or "gpt-6.1-sol"
    brief = Path(os.environ.get("FACTORY_IN", "/factory/in")) / "brief" / "brief.json"
    role = os.environ.get("FACTORY_ROLE", "")
    try:
        if brief.is_file() and role != "foreman":
            text = _still(brief, model)
        elif role == "foreman":
            text = _foreman(model, _flag("-p") or "")
        else:
            text = _chat(model, _flag("-p") or "", "你在操作 OpenMontage。回答要短，不要解释。")
    except Exception as exc:  # noqa: BLE001 — 操作工要把失败写成 transcript
        _result(str(exc), error=True)
        print(str(exc), file=sys.stderr)
        return 1
    _result(text, error=False)
    return 0


def _still(path: Path, model: str) -> str:
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise RuntimeError("brief.json 不是对象")
    source = str(body.get("prompt") or "").strip()
    if not source:
        raise RuntimeError("brief.json 缺少 prompt")
    prompt = _chat(
        model,
        "把下面的画面收成一条英文静帧提示词，只输出提示词本身。\n\n" + source,
        "你在操作 OpenMontage。回答要短，不要解释。",
    ).strip().strip("`\"'")
    if not prompt:
        raise RuntimeError("模型没有返回提示词")
    image = _image(prompt)
    dest = Path(os.environ.get("FACTORY_OUT", "/factory/out")) / "image" / "image.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(image)
    return prompt


def _foreman(model: str, user: str) -> str:
    text = _chat(model, user, "你是工厂的常驻厂长。用一两句中文直接回答人。", max_tokens=120)
    if os.environ.get("FACTORY_AGENT") == "foreman":
        _talk_reply(text)
    return text


def _talk_reply(text: str) -> None:
    ctl = Path(os.environ.get("FACTORY_CTL", ""))
    if not ctl.is_dir():
        raise RuntimeError("没有 FACTORY_CTL")
    _mcp(
        ctl,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "openai-sol", "version": "1"}},
    )
    found = _mcp(ctl, "tools/call", {"name": "talk_reply", "arguments": {"text": text}})
    result = found.get("result") if isinstance(found, dict) else None
    if not isinstance(result, dict) or result.get("isError"):
        raise RuntimeError("talk_reply 失败：" + json.dumps(found, ensure_ascii=False)[:400])


def _mcp(ctl: Path, method: str, params: dict[str, object]) -> dict[str, object]:
    rid = "r-" + uuid.uuid4().hex[:8]
    body = {
        "id": rid,
        "op": "mcp",
        "args": {
            "message": {
                "jsonrpc": "2.0",
                "id": rid,
                "method": method,
                "params": params,
            }
        },
    }
    dest = ctl / "requests" / f"{rid}.json"
    tmp = dest.with_suffix(".part")
    tmp.write_text(json.dumps(body), encoding="utf-8")
    os.replace(tmp, dest)
    response = ctl / "responses" / f"{rid}.json"
    deadline = time.monotonic() + 15
    while not response.is_file():
        if time.monotonic() > deadline:
            raise RuntimeError(f"MCP {method} 没有回应")
        time.sleep(0.05)
    found = json.loads(response.read_text(encoding="utf-8"))
    if not isinstance(found, dict) or not found.get("ok"):
        raise RuntimeError(f"MCP {method} 失败：" + json.dumps(found, ensure_ascii=False)[:400])
    message = (found.get("result") or {}).get("message")
    if not isinstance(message, dict):
        raise RuntimeError(f"MCP {method} 没有消息")
    if message.get("error"):
        raise RuntimeError(f"MCP {method} 错误：" + json.dumps(message["error"], ensure_ascii=False)[:400])
    return message


def _chat(model: str, user: str, system: str, *, max_tokens: int = 80) -> str:
    proxy = os.environ.get("MODEL_PROXY", "").rstrip("/")
    if not proxy:
        raise RuntimeError("没有 MODEL_PROXY")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Factory-Job": os.environ.get("FACTORY_JOB_ID", ""),
    }
    found = _post(proxy + "/v1/chat/completions", payload, headers, bypass_proxy=True, timeout=80)
    choices = found.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("模型没有返回 choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("模型没有返回文本")
    return content.strip()


def _image(prompt: str) -> bytes:
    key = os.environ.get("TBTK_API_KEY", "")
    if not key:
        raise RuntimeError("没有 TBTK_API_KEY")
    payload = {"model": IMAGE_MODEL, "prompt": prompt, "size": "1024x1024", "n": 1}
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    found = _post(IMAGE_URL, payload, headers, bypass_proxy=False, timeout=120)
    data = found.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise RuntimeError("生图接口没有返回 data")
    raw = data[0].get("b64_json")
    if not isinstance(raw, str) or not raw:
        raise RuntimeError("生图接口没有返回 b64_json")
    return base64.b64decode(raw)


def _post(
    url: str,
    payload: dict[str, object],
    headers: dict[str, str],
    *,
    bypass_proxy: bool,
    timeout: float,
) -> dict[str, object]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if bypass_proxy else urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    if not isinstance(body, dict):
        raise RuntimeError("响应不是 JSON 对象")
    return body


def _result(text: str, *, error: bool) -> None:
    event = {
        "type": "result",
        "subtype": "error_during_execution" if error else "success",
        "is_error": error,
        "duration_ms": 0,
        "num_turns": 1,
        "result": text,
        "session_id": os.environ.get("FACTORY_JOB_ID", ""),
        "total_cost_usd": 0,
    }
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _flag(name: str) -> str | None:
    if name not in sys.argv:
        return None
    index = sys.argv.index(name)
    if index + 1 >= len(sys.argv):
        return None
    return sys.argv[index + 1]


if __name__ == "__main__":
    sys.exit(main())
