"""gpt-6.1-sol 操作工。stdout 只写 stream-json。

生图工位：先让模型把 brief 收成一条提示词，再按 OpenMontage openai_image
的 images.generate 契约，把图交给 tbtk.asia。跳舞工位先让模型写动作提示，
再调用旁边的 MiniMax 机器。厂长可以读写 Registry、提交，并用 MCP 提议变更。
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
import uuid
from pathlib import Path

IMAGE_URL = "https://tbtk.asia/v1/images/generations"
IMAGE_MODEL = "gpt-image-2.5"


def main() -> int:
    model = _flag("--model") or "gpt-6.1-sol"
    brief = Path(os.environ.get("FACTORY_IN", "/factory/in")) / "brief" / "brief.json"
    role = os.environ.get("FACTORY_ROLE", "")
    try:
        incoming = Path(os.environ.get("FACTORY_IN", "/factory/in"))
        audio = _media(incoming, (".mp3", ".wav", ".flac"))
        image = _media(incoming, (".png", ".jpg", ".jpeg", ".webp"))
        motion = _media(incoming, (".mp4",))
        if role == "foreman":
            text = _foreman(model, _flag("-p") or "")
        elif motion is not None and image is not None:
            text = _grok(model, brief if brief.is_file() else None)
        elif audio is not None and image is not None:
            text = _dance(model, brief if brief.is_file() else None)
        elif brief.is_file():
            text = _still(brief, model)
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


def _grok(model: str, brief: Path | None) -> str:
    source = ""
    if brief is not None:
        source = brief.read_text(encoding="utf-8")
    prompt = _chat(
        model,
        "参考视频里的舞蹈、镜头、节奏和背景都要保留，只把跳舞的人换成说明里的角色。"
        "这是 5 秒试片。写一条给 Grok 视频编辑的中文提示，只输出提示词。\n\n" + source,
        "你在操作 Grok 视频编辑。回答只要提示词。",
        max_tokens=180,
    ).strip().strip("`\"'")
    if not prompt:
        raise RuntimeError("模型没有写出动作提示")
    env = dict(os.environ)
    env["DANCE_PROMPT"] = prompt
    machine = Path(__file__).with_name("grok_video.py")
    proc = subprocess.run([sys.executable, str(machine)], env=env, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"视频机器退出 {proc.returncode}")
    return prompt


def _dance(model: str, brief: Path | None) -> str:
    source = ""
    if brief is not None:
        source = brief.read_text(encoding="utf-8")
    prompt = _chat(
        model,
        "根据下面的说明，写一条给视频模型的中文动作提示。要保留人物设定，强调可爱体型和 jumpstyle 硬舞的反差。只输出提示词。\n\n" + source,
        "你在操作 MiniMax 视频机器。回答只要提示词。",
        max_tokens=180,
    ).strip().strip("`\"'")
    if not prompt:
        raise RuntimeError("模型没有写出动作提示")
    env = dict(os.environ)
    env["DANCE_PROMPT"] = prompt
    machine = Path(__file__).with_name("minimax_h3.py")
    proc = subprocess.run([sys.executable, str(machine)], env=env, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"视频机器退出 {proc.returncode}")
    return prompt


def _media(folder: Path, suffixes: tuple[str, ...]) -> Path | None:
    if not folder.is_dir():
        return None
    files = [
        path
        for path in sorted(folder.rglob("*"))
        if path.is_file() and path.suffix.lower() in suffixes
    ]
    return files[0] if files else None


def _foreman(model: str, user: str) -> str:
    if os.environ.get("FACTORY_AGENT") != "foreman":
        return _chat(model, user, "你是工厂的常驻厂长。用一两句中文直接回答人。", max_tokens=120)
    _mcp(
        _ctl(),
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "openai-sol", "version": "1"}},
    )
    tools = _factory_tools()
    messages: list[dict[str, object]] = [
        {
            "role": "system",
            "content": (
                "你是常驻厂长。工作区根下有 registry/，那是本厂 Registry 的克隆。"
                "只添加文件，不要改已有对象。人指定了策略、命令、端口和对象名字时，按人的话写，不要改回旧的 MiniMax 配置。"
                "先读一条现有的 machine、station、line，对照字段。Station 的 spec 不能写 instructions。"
                "Type 版本用 \"1.0\"。Station 的 role 用 transform，除非人指定别的。"
                "写完后 git_commit，用返回的完整 commit 调 factory registry_check。检查失败就改文件再提交。"
                "然后 factory epoch_show，用返回的 epoch 作为 base_epoch，factory changeset_propose。"
                "changes 只列出这次真正新增的对象，op 用 upsert。"
                "rollout 用 shadow.runs=0、shadow.budget_usd=1、canary.percent=100、canary.min_runs=1、observe=0s，不要写 slo。"
                "最后必须 factory talk_reply，用中文说明变更号、产线名字和输入端口。"
            ),
        },
        {"role": "user", "content": user},
    ]
    replied = ""
    for _ in range(12):
        message = _chat_tools(model, messages, tools)
        calls = message.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            text = str(message.get("content") or "").strip()
            _talk_reply(text or "这一轮没有写成产线。")
            return text or "这一轮没有写成产线。"
        messages.append(message)
        for call in calls:
            if not isinstance(call, dict):
                continue
            result = _use_tool(call)
            if call.get("function", {}).get("name") == "talk_reply":
                replied = result
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or ""),
                    "content": result[:4000],
                }
            )
        if replied:
            return replied
    _talk_reply("这一轮还没写完。")
    return "这一轮还没写完。"


def _factory_tools() -> list[dict[str, object]]:
    local = [
        _function(
            "list_dir",
            "列出工作区里的一层目录",
            {"path": {"type": "string"}},
            ["path"],
        ),
        _function(
            "read_file",
            "读取工作区里的文本文件",
            {"path": {"type": "string"}},
            ["path"],
        ),
        _function(
            "write_file",
            "在工作区里写入文本文件",
            {"path": {"type": "string"}, "content": {"type": "string"}},
            ["path", "content"],
        ),
        _function("git_commit", "提交工作区里的 Registry 改动，返回 commit", {"message": {"type": "string"}}, ["message"]),
    ]
    listed = _mcp(_ctl(), "tools/list", {})
    result = listed.get("result") if isinstance(listed, dict) else None
    tools = result.get("tools") if isinstance(result, dict) else None
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict) or not tool.get("name"):
                continue
            schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {"type": "object"}
            local.append(
                _function(
                    "factory_" + str(tool["name"]),
                    str(tool.get("description") or tool["name"]),
                    schema.get("properties") if isinstance(schema.get("properties"), dict) else {},
                    schema.get("required") if isinstance(schema.get("required"), list) else [],
                )
            )
    return local


def _function(name: str, description: str, properties: dict[str, object], required: list[object]) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": [str(item) for item in required],
            },
        },
    }


def _use_tool(call: dict[str, object]) -> str:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = str(function.get("name") or "")
    raw = function.get("arguments") if isinstance(function, dict) else {}
    if isinstance(raw, dict):
        args = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            return "参数不是 JSON"
    else:
        args = {}
    if not isinstance(args, dict):
        return "参数不是对象"
    try:
        if name == "list_dir":
            folder = _workspace(str(args.get("path") or "."))
            names = sorted(item.name for item in folder.iterdir()) if folder.is_dir() else []
            return "\n".join(names)
        if name == "read_file":
            return _workspace(str(args.get("path") or "")).read_text(encoding="utf-8")[:6000]
        if name == "write_file":
            dest = _workspace(str(args.get("path") or ""))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(str(args.get("content") or ""), encoding="utf-8")
            return f"wrote {dest.name}"
        if name == "git_commit":
            return _git_commit(str(args.get("message") or "foreman line"))
        if name.startswith("factory_"):
            found = _mcp(_ctl(), "tools/call", {"name": name[len("factory_") :], "arguments": args})
            return json.dumps(found, ensure_ascii=False)[:4000]
    except Exception as exc:  # noqa: BLE001 — 工具错误要回给模型
        return str(exc)
    return "没有这个工具"


def _workspace(path: str) -> Path:
    root = Path(os.environ.get("FACTORY_WORKSPACE", "/workspace")).resolve()
    raw = path or "."
    if raw.startswith("/workspace"):
        raw = str(root) + raw[len("/workspace") :]
    dest = Path(raw)
    if not dest.is_absolute():
        dest = root / dest
    dest = dest.resolve()
    if dest != root and root not in dest.parents:
        raise RuntimeError("路径不在工作区")
    return dest


def _git_commit(message: str) -> str:
    repo = _workspace("registry")
    if not (repo / ".git").exists():
        repo = _workspace(".")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    dirty = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo, check=False)
    if dirty.returncode != 0:
        subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
    current = head.stdout.strip()
    active = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/factory/active"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if active.returncode != 0:
        return current
    base = active.stdout.strip()
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, current], cwd=repo, check=False
    )
    if ancestor.returncode == 0:
        return current
    tree = subprocess.run(
        ["git", "rev-parse", f"{current}^{{tree}}"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    rebased = subprocess.run(
        ["git", "commit-tree", tree, "-p", base, "-m", message],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "checkout", "--detach", rebased], cwd=repo, check=True)
    return rebased


def _chat_tools(model: str, messages: list[dict[str, object]], tools: list[dict[str, object]]) -> dict[str, object]:
    proxy = os.environ.get("MODEL_PROXY", "").rstrip("/")
    if not proxy:
        raise RuntimeError("没有 MODEL_PROXY")
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "max_tokens": 1200,
        "temperature": 0,
    }
    headers = {"Content-Type": "application/json", "X-Factory-Job": os.environ.get("FACTORY_JOB_ID", "")}
    found = _post(proxy + "/v1/chat/completions", payload, headers, bypass_proxy=True, timeout=80)
    choices = found.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError("模型没有返回 choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise RuntimeError("模型没有返回消息")
    return message


def _ctl() -> Path:
    ctl = Path(os.environ.get("FACTORY_CTL", ""))
    if not ctl.is_dir():
        raise RuntimeError("没有 FACTORY_CTL")
    return ctl


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
    deadline = time.monotonic() + 60
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
