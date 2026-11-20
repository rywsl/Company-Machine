"""文件邮箱：轮询请求，调用 handler，原子写响应。"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from mao.base import SystemClock


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)


class CtlServer:
    def __init__(self, ctl_dir: Path, handler: Any, clock: Any = None) -> None:
        self.ctl_dir = Path(ctl_dir)
        self.handler = handler
        self.clock = clock if clock is not None else SystemClock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current_id = ""
        self._lock = threading.Lock()

    def poll_once(self) -> int:
        requests = self.ctl_dir / "requests"
        if not requests.is_dir():
            return 0
        written = 0
        for path in sorted(requests.glob("*.json")):
            stem = path.stem
            response_path = self.ctl_dir / "responses" / f"{stem}.json"
            if response_path.is_file():
                continue
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                body = None
            if (
                not isinstance(body, dict)
                or not isinstance(body.get("id"), str)
                or not isinstance(body.get("op"), str)
                or body.get("id") != stem
            ):
                _atomic_json(
                    response_path,
                    {
                        "id": stem,
                        "ok": False,
                        "result": None,
                        "error": {"code": "CTL_REQUEST_INVALID", "message": "请求文件不合法"},
                    },
                )
                written += 1
                continue
            with self._lock:
                self._current_id = stem
            try:
                result = self.handler.handle(body)
            except Exception:
                with self._lock:
                    self._current_id = stem
                raise
            if result is None:
                continue
            _atomic_json(response_path, result)
            written += 1
        return written

    def start(self, interval_s: float = 0.2) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("CtlServer 已经启动")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(interval_s,), name="mao-ctl", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(5)
        if self._thread.is_alive():
            raise RuntimeError("CtlServer 线程没有在 5 秒内退出")
        self._thread = None

    def _loop(self, interval_s: float) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                logging.getLogger("mao.ctl").exception("poll_once 失败")
                self._write_handler_error()
            if self._stop.wait(interval_s):
                return

    def _write_handler_error(self) -> None:
        with self._lock:
            rid = self._current_id
        if not rid:
            return
        _atomic_json(
            self.ctl_dir / "responses" / f"{rid}.json",
            {
                "id": rid,
                "ok": False,
                "result": None,
                "error": {"code": "CTL_HANDLER_ERROR", "message": "处理请求时发生异常"},
            },
        )
