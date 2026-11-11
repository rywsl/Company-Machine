#!/usr/bin/env python3
"""测试替身：脚本化的模型上游（spec/system/runtimes.md 第 7 章、live.md 第 7 章）。

实现两种 API 的最小子集，足以让真实的 agent CLI 跑完一次会话：
- Anthropic Messages：POST /v1/messages（stream 或非 stream）、POST /v1/messages/count_tokens
- OpenAI Responses：POST /v1/responses（stream 或非 stream）

脚本（JSON 文件）：

    {
      "usage": {"input_tokens": 100, "output_tokens": 50},  # 每次响应报告的用量
      "main_models": ["fake-opus"],                         # 只有这些模型的请求按轮次取 turns
      "turns": [[块...], [块...]],                           # 第 n 轮回复的内容块
      "fallback": "ok"                                       # 次要模型、轮次用完时的文本回复
    }

脚本也可以分 API 写：{"anthropic": {上面的键...}, "openai": {上面的键...}}。
某个 API 没有自己的段时用顶层的键。

块：
    {"text": "..."}
    {"tool": {"suffix": "changeset_propose"}, "input": {...}}  在请求的 tools 里找名字以 suffix 结尾的唯一工具
    {"tool": {"name": "Bash"}, "input": {...}}                 按全名找

input 里的字符串替换（递归）：
    "@last_result"       请求里最后一条工具结果的文本（去首尾空白）
    "@prompt:<正则>"      全部用户消息的文本（不含工具结果）里正则的第一个分组

轮次：
    Anthropic：请求 messages 里 assistant 消息的条数（无状态）。
    OpenAI：按全部用户消息的文本分会话，每个主模型请求加一（有状态）。

每个请求在 FakeLlm.requests 里留一条记录：
    {"path", "model", "headers"（小写名）, "turn", "stream", "last_result", "prompt"}
其中 last_result 是请求里最后一条工具结果的文本，prompt 是全部用户消息的文本（不含工具结果）。
只用标准库。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class ScriptError(Exception):
    pass


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("text", "input_text", "output_text"):
                parts.append(str(block.get("text", "")))
            elif isinstance(block, dict) and block.get("type") == "tool_result":
                parts.append(_text_of(block.get("content", "")))
        return "\n".join(parts)
    return ""


def _prompt_of(content: Any) -> str:
    """用户消息里的文本，不含工具结果。"""
    if isinstance(content, list):
        content = [b for b in content if not (isinstance(b, dict) and b.get("type") == "tool_result")]
    return _text_of(content)


class FakeLlm:
    def __init__(self, script: Path) -> None:
        self.script_path = Path(script)
        self.requests: list[dict[str, Any]] = []
        self._counters: dict[str, int] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 生命周期
    def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                owner._handle(self)

            def do_GET(self) -> None:  # noqa: N802
                owner._send_json(self, 404, {"error": {"type": "not_found", "message": self.path}})

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-llm", daemon=True)
        self._thread.start()
        return self._server.server_address[1]

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # ------------------------------------------------------------------ 请求
    def _handle(self, req: BaseHTTPRequestHandler) -> None:
        length = int(req.headers.get("Content-Length") or 0)
        try:
            body = json.loads(req.rfile.read(length) or b"{}")
        except ValueError:
            self._send_json(req, 400, {"error": {"type": "invalid_request_error", "message": "bad json"}})
            return
        path = req.path.split("?", 1)[0]
        headers = {k.lower(): v for k, v in req.headers.items()}
        if path == "/v1/messages/count_tokens":
            self._record(path, body, headers, None, False)
            self._send_json(req, 200, {"input_tokens": 10})
            return
        try:
            script = json.loads(self.script_path.read_text(encoding="utf-8"))
            if path == "/v1/messages":
                self._anthropic(req, path, body, headers, script.get("anthropic", script))
            elif path in ("/v1/responses", "/responses"):
                self._openai(req, path, body, headers, script.get("openai", script))
            else:
                self._send_json(req, 404, {"error": {"type": "not_found", "message": path}})
        except ScriptError as exc:
            self._send_json(req, 500, {"error": {"type": "api_error", "message": f"fake-llm: {exc}"}})

    def _record(
        self,
        path: str,
        body: dict,
        headers: dict,
        turn: int | None,
        stream: bool,
        last: str = "",
        prompt: str = "",
    ) -> None:
        with self._lock:
            self.requests.append(
                {
                    "path": path,
                    "model": body.get("model"),
                    "headers": headers,
                    "turn": turn,
                    "stream": stream,
                    "last_result": last,
                    "prompt": prompt,
                }
            )

    # ------------------------------------------------------------------ 脚本
    def _blocks(
        self, script: dict, model: str, turn: int, tools: list[str], last: str, prompt: str
    ) -> list[dict]:
        main = script.get("main_models") or []
        turns = script.get("turns") or []
        if (main and model not in main) or turn >= len(turns):
            return [{"type": "text", "text": str(script.get("fallback", "ok"))}]
        out = []
        for raw in turns[turn]:
            if "text" in raw:
                out.append({"type": "text", "text": str(raw["text"])})
                continue
            spec = raw.get("tool") or {}
            if "name" in spec:
                matches = [name for name in tools if name == spec["name"]]
            else:
                matches = [name for name in tools if name.endswith(str(spec.get("suffix", "")))]
            if len(matches) != 1:
                raise ScriptError(f"tool {spec} matched {matches} among {sorted(tools)}")
            value = self._substitute(raw.get("input", {}), last, prompt)
            out.append({"type": "tool", "name": matches[0], "input": value})
        return out

    def _substitute(self, value: Any, last: str, prompt: str) -> Any:
        if isinstance(value, dict):
            return {k: self._substitute(v, last, prompt) for k, v in value.items()}
        if isinstance(value, list):
            return [self._substitute(v, last, prompt) for v in value]
        if value == "@last_result":
            return last.strip()
        if isinstance(value, str) and value.startswith("@prompt:"):
            found = re.search(value[len("@prompt:") :], prompt)
            if not found:
                raise ScriptError(f"prompt does not match {value!r}")
            return found.group(1)
        return value

    # ------------------------------------------------------------------ Anthropic
    def _anthropic(self, req, path: str, body: dict, headers: dict, script: dict) -> None:
        messages = body.get("messages") or []
        model = str(body.get("model", ""))
        turn = sum(1 for m in messages if m.get("role") == "assistant")
        tools = [str(t.get("name")) for t in body.get("tools") or [] if isinstance(t, dict)]
        users = [m for m in messages if m.get("role") == "user"]
        prompt = "\n".join(_prompt_of(m.get("content")) for m in users)
        last = ""
        if users and isinstance(users[-1].get("content"), list):
            results = [
                b for b in users[-1]["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
            ]
            if results:
                last = _text_of(results[-1].get("content", ""))
        stream = bool(body.get("stream"))
        self._record(path, body, headers, turn, stream, last, prompt)
        blocks = self._blocks(script, model, turn, tools, last, prompt)
        usage = script.get("usage") or {"input_tokens": 100, "output_tokens": 50}
        content = []
        for i, block in enumerate(blocks):
            if block["type"] == "text":
                content.append({"type": "text", "text": block["text"]})
            else:
                content.append(
                    {
                        "type": "tool_use",
                        "id": f"toolu_{turn}_{i}",
                        "name": block["name"],
                        "input": block["input"],
                    }
                )
        stop = "tool_use" if any(c["type"] == "tool_use" for c in content) else "end_turn"
        if not stream:
            self._send_json(
                req,
                200,
                {
                    "id": f"msg_{turn}",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": content,
                    "stop_reason": stop,
                    "stop_sequence": None,
                    "usage": {"input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"]},
                },
            )
            return
        events: list[tuple[str, dict]] = [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": f"msg_{turn}",
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": usage["input_tokens"], "output_tokens": 1},
                    },
                },
            )
        ]
        for i, block in enumerate(content):
            if block["type"] == "text":
                start = {"type": "text", "text": ""}
                delta = {"type": "text_delta", "text": block["text"]}
            else:
                start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
                delta = {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(block["input"], ensure_ascii=False),
                }
            events.append(
                ("content_block_start", {"type": "content_block_start", "index": i, "content_block": start})
            )
            events.append(
                ("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta})
            )
            events.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
        events.append(
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop, "stop_sequence": None},
                    "usage": {"output_tokens": usage["output_tokens"]},
                },
            )
        )
        events.append(("message_stop", {"type": "message_stop"}))
        self._send_sse(req, events)

    # ------------------------------------------------------------------ OpenAI Responses
    def _openai(self, req, path: str, body: dict, headers: dict, script: dict) -> None:
        items = body.get("input") or []
        if isinstance(items, str):
            items = [{"type": "message", "role": "user", "content": items}]
        model = str(body.get("model", ""))
        users = [i for i in items if i.get("type", "message") == "message" and i.get("role") == "user"]
        prompt = "\n".join(_prompt_of(i.get("content")) for i in users)
        outputs = [i for i in items if i.get("type") == "function_call_output"]
        last = str(outputs[-1].get("output", "")) if outputs else ""
        main = script.get("main_models") or []
        key = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        with self._lock:
            turn = self._counters.get(key, 0)
            if not main or model in main:
                self._counters[key] = turn + 1
        tools = [str(t.get("name")) for t in body.get("tools") or [] if isinstance(t, dict) and t.get("name")]
        stream = bool(body.get("stream"))
        self._record(path, body, headers, turn, stream, last, prompt)
        blocks = self._blocks(script, model, turn, tools, last, prompt)
        usage = script.get("usage") or {"input_tokens": 100, "output_tokens": 50}
        output = []
        for i, block in enumerate(blocks):
            if block["type"] == "text":
                output.append(
                    {
                        "id": f"msg_{turn}_{i}",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": block["text"], "annotations": []}],
                    }
                )
            else:
                output.append(
                    {
                        "id": f"fc_{turn}_{i}",
                        "type": "function_call",
                        "status": "completed",
                        "call_id": f"call_{turn}_{i}",
                        "name": block["name"],
                        "arguments": json.dumps(block["input"], ensure_ascii=False),
                    }
                )
        response = {
            "id": f"resp_{turn}",
            "object": "response",
            "status": "completed",
            "model": model,
            "output": output,
            "usage": {
                "input_tokens": usage["input_tokens"],
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": usage["output_tokens"],
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": usage["input_tokens"] + usage["output_tokens"],
            },
        }
        if not stream:
            self._send_json(req, 200, response)
            return
        created = dict(response, status="in_progress", output=[], usage=None)
        events: list[tuple[str, dict]] = [
            ("response.created", {"type": "response.created", "response": created})
        ]
        for i, item in enumerate(output):
            pending = dict(item, status="in_progress")
            if item["type"] == "message":
                pending["content"] = []
            else:
                pending["arguments"] = ""
            events.append(
                (
                    "response.output_item.added",
                    {"type": "response.output_item.added", "output_index": i, "item": pending},
                )
            )
            if item["type"] == "message":
                events.append(
                    (
                        "response.output_text.delta",
                        {
                            "type": "response.output_text.delta",
                            "item_id": item["id"],
                            "output_index": i,
                            "content_index": 0,
                            "delta": item["content"][0]["text"],
                        },
                    )
                )
            else:
                events.append(
                    (
                        "response.function_call_arguments.delta",
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": item["id"],
                            "output_index": i,
                            "delta": item["arguments"],
                        },
                    )
                )
            events.append(
                (
                    "response.output_item.done",
                    {"type": "response.output_item.done", "output_index": i, "item": item},
                )
            )
        events.append(("response.completed", {"type": "response.completed", "response": response}))
        self._send_sse(req, events)

    # ------------------------------------------------------------------ 输出
    def _send_json(self, req, status: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        req.send_response(status)
        req.send_header("Content-Type", "application/json")
        req.send_header("Content-Length", str(len(data)))
        req.end_headers()
        req.wfile.write(data)

    def _send_sse(self, req, events: list[tuple[str, dict]]) -> None:
        data = "".join(
            f"event: {name}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n" for name, obj in events
        ).encode("utf-8")
        req.send_response(200)
        req.send_header("Content-Type", "text/event-stream")
        req.send_header("Content-Length", str(len(data)))
        req.end_headers()
        req.wfile.write(data)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write("usage: fake_llm.py SCRIPT PORT\n")
        return 2
    server = FakeLlm(Path(argv[1]))
    port = server.start(port=int(argv[2]))
    sys.stdout.write(f"{port}\n")
    sys.stdout.flush()
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
