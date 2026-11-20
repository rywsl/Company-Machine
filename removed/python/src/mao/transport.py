"""有界传输、地址校验与未知请求记录（spec/system/operability.md 第 3.1、5.1、5.2 节）。"""

from __future__ import annotations

import json
import select
import socket
import ssl
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from mao.base import utc_iso
from mao.errors import FactoryError

_DEFAULT = {
    "model": {"connect_s": 10.0, "idle_s": 90.0, "total_s": 300.0},
    "egress": {"connect_s": 10.0, "idle_s": 300.0, "total_s": 900.0, "max_tunnels": 64},
    "tls_inspection": None,
}


@dataclass(frozen=True)
class Deadline:
    connect_s: float
    idle_s: float
    total_s: float


def load_transport(root: Path) -> Mapping[str, object]:
    """读取并规范化 factory.json.transport。不写文件，不打开模型连接。"""
    path = Path(root) / "factory.json"
    section: dict[str, Any] = {}
    if path.is_file():
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FactoryError(
                "TRANSPORT_CONFIG_INVALID", "factory.json 无法解析", field="transport"
            ) from exc
        if isinstance(body, dict) and "transport" in body:
            raw = body.get("transport")
            if not isinstance(raw, dict):
                raise FactoryError("TRANSPORT_CONFIG_INVALID", "transport 必须是对象", field="transport")
            section = raw
    model = _deadline_section(section.get("model"), _DEFAULT["model"], "model")
    egress = dict(_DEFAULT["egress"])
    if "egress" in section:
        given = section.get("egress")
        if not isinstance(given, dict):
            raise FactoryError("TRANSPORT_CONFIG_INVALID", "egress 必须是对象", field="egress")
        unknown = set(given) - {"connect_s", "idle_s", "total_s", "max_tunnels"}
        if unknown:
            raise FactoryError("TRANSPORT_CONFIG_INVALID", "egress 有未知字段", field=sorted(unknown)[0])
        egress.update(given)
    egress_deadline = _deadline_section(
        {key: egress[key] for key in ("connect_s", "idle_s", "total_s")},
        _DEFAULT["egress"],
        "egress",
    )
    tunnels = egress.get("max_tunnels", 64)
    if isinstance(tunnels, bool) or not isinstance(tunnels, int) or tunnels <= 0:
        raise FactoryError("TRANSPORT_CONFIG_INVALID", "max_tunnels 必须是正整数", field="max_tunnels")
    inspection = section.get("tls_inspection", None)
    if "tls_inspection" in section:
        inspection = _inspection(section.get("tls_inspection"))
    unknown = set(section) - {"model", "egress", "tls_inspection"}
    if unknown:
        raise FactoryError("TRANSPORT_CONFIG_INVALID", "transport 有未知字段", field=sorted(unknown)[0])
    return {
        "model": model,
        "egress": {
            "connect_s": egress_deadline.connect_s,
            "idle_s": egress_deadline.idle_s,
            "total_s": egress_deadline.total_s,
            "max_tunnels": int(tunnels),
        },
        "tls_inspection": inspection,
    }


def resolve_service_location(location: str, *, base_url: str, allowed_origins: Sequence[str]) -> str:
    """规范化服务地址。绝对地址和相对地址都要落在允许的 origin 内。"""
    if not isinstance(location, str) or not location or any(ord(ch) < 32 for ch in location):
        raise FactoryError("SERVICE_LOCATION_DENIED", "地址不合法", field="location")
    if location.lower().startswith("file:"):
        raise FactoryError("SERVICE_LOCATION_DENIED", "不允许文件地址", field="location")
    base = urlsplit(base_url)
    if base.scheme not in ("http", "https") or not base.hostname or base.username or base.password:
        raise FactoryError("SERVICE_LOCATION_DENIED", "基准地址不合法", field="base_url")
    target = urlsplit(location)
    if target.username or target.password:
        raise FactoryError("SERVICE_LOCATION_DENIED", "地址不能带用户信息", field="location")
    if target.scheme == "" and target.netloc == "":
        path = location if location.startswith("/") else "/" + location
        resolved = urlunsplit((base.scheme, base.netloc, path.split("?", 1)[0], "", ""))
        if "?" in location:
            resolved = resolved + "?" + location.split("?", 1)[1]
        target = urlsplit(resolved)
    if target.scheme not in ("http", "https") or not target.hostname:
        raise FactoryError("SERVICE_LOCATION_DENIED", "地址方案不合法", field="location")
    origin = _origin(target.scheme, target.hostname, target.port)
    allowed = {_canonical_origin(item) for item in allowed_origins}
    if origin not in allowed:
        raise FactoryError("SERVICE_LOCATION_DENIED", "地址超出允许来源", field="location")
    return urlunsplit((target.scheme, target.netloc, target.path or "/", target.query, ""))


def effective_deadline(configured: Deadline, remaining_s: float | None) -> Deadline:
    if remaining_s is None:
        return configured
    return Deadline(configured.connect_s, configured.idle_s, min(configured.total_s, max(0.0, remaining_s)))


def model_deadline(root: Path, remaining_s: float | None = None) -> Deadline:
    section = load_transport(root)["model"]
    assert isinstance(section, Deadline)
    return effective_deadline(section, remaining_s)


def egress_limits(root: Path | None) -> tuple[Deadline, int, dict[str, Any] | None]:
    if root is None:
        deadline = Deadline(10.0, 300.0, 900.0)
        return deadline, 64, None
    loaded = load_transport(root)
    egress = loaded["egress"]
    assert isinstance(egress, dict)
    deadline = Deadline(float(egress["connect_s"]), float(egress["idle_s"]), float(egress["total_s"]))
    inspection = loaded["tls_inspection"]
    return deadline, int(egress["max_tunnels"]), inspection if isinstance(inspection, dict) else None


class TunnelSlots:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._active = 0
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        with self._lock:
            if self._active >= self.limit:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    @property
    def active(self) -> int:
        with self._lock:
            return self._active


def http_exchange(
    url: str,
    data: bytes,
    headers: Mapping[str, str],
    deadline: Deadline,
    *,
    clock: Any,
) -> tuple[int, bytes, str]:
    """按 connect/idle/total 发送一次 HTTP 请求。超时不重试。"""
    started = clock.monotonic()
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    try:
        sock = socket.create_connection(
            (host, port), timeout=min(deadline.connect_s, _remaining(deadline, clock, started))
        )
    except OSError as exc:
        raise _timeout("connect", "not_sent", True, 0) from exc
    bytes_sent = 0
    try:
        if parsed.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        sock.settimeout(min(deadline.connect_s, _remaining(deadline, clock, started)))
        request = _http_bytes("POST", path, host, port, headers, data)
        sock.sendall(request)
        bytes_sent = len(request)
        status, payload, content_type = _read_http(sock, deadline, clock, started, bytes_sent)
        return status, payload, content_type
    except FactoryError:
        raise
    except OSError as exc:
        outcome, retryable = ("not_sent", True) if bytes_sent == 0 else ("outcome_unknown", False)
        phase = "connect" if bytes_sent == 0 else "response"
        raise _timeout(phase, outcome, retryable, bytes_sent) from exc
    finally:
        try:
            sock.close()
        except OSError:
            pass


def record_attempt(
    db_path: Path,
    *,
    request_id: str,
    operation_id: str,
    principal_kind: str,
    principal_id: str,
    phase: str,
    outcome: str,
    bytes_sent: int,
    deadline_at: str,
    error_code: str | None,
    usage_ref: str | None,
    at: str,
) -> None:
    if not db_path.is_file():
        return
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO transport_attempts (
                   request_id, operation_id, principal_kind, principal_id, origin_id, phase, outcome,
                   bytes_sent, deadline_at, error_code, usage_ref, created_at, updated_at
               ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(request_id) DO UPDATE SET
                   phase = excluded.phase, outcome = excluded.outcome, bytes_sent = excluded.bytes_sent,
                   error_code = excluded.error_code, usage_ref = excluded.usage_ref, updated_at = excluded.updated_at""",
            (
                request_id,
                operation_id,
                principal_kind,
                principal_id,
                phase,
                outcome,
                int(bytes_sent),
                deadline_at,
                error_code,
                usage_ref,
                at,
                at,
            ),
        )
        conn.commit()
    except sqlite3.OperationalError:
        return
    finally:
        conn.close()


def unknown_blocks(db_path: Path, operation_id: str) -> bool:
    if not operation_id or not db_path.is_file():
        return False
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM transport_attempts WHERE operation_id = ? AND outcome = 'outcome_unknown'",
            (operation_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()
    return row is not None


def take_buffered(reader: Any, sock: socket.socket) -> bytes:
    """只取已经进入缓冲或立即可读的字节，不等待可选后续字节。"""
    sock.setblocking(False)
    try:
        try:
            peeked = reader.peek(65536)
        except (BlockingIOError, ssl.SSLWantReadError, OSError):
            peeked = b""
        if peeked:
            return reader.read(len(peeked))
        return b""
    finally:
        sock.setblocking(True)


def relay_until(
    left: socket.socket, right: socket.socket, deadline: Deadline, clock: Any, cancel: threading.Event
) -> None:
    started = clock.monotonic()
    last = started
    sockets = [left, right]
    for item in sockets:
        item.setblocking(False)
    try:
        while not cancel.is_set():
            now = clock.monotonic()
            if now - started >= deadline.total_s or now - last >= deadline.idle_s:
                return
            wait = min(deadline.idle_s - (now - last), deadline.total_s - (now - started), 0.05)
            readable, _, _ = select.select(sockets, [], [], max(0.0, wait))
            if not readable:
                continue
            for item in readable:
                other = right if item is left else left
                try:
                    chunk = item.recv(65536)
                except (BlockingIOError, ssl.SSLWantReadError):
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                other.sendall(chunk)
                last = clock.monotonic()
    finally:
        for item in sockets:
            try:
                item.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                item.close()
            except OSError:
                pass


def inspection_cert(config: Mapping[str, Any] | None, host: str) -> tuple[str, str] | None:
    if not isinstance(config, dict):
        return None
    hosts = config.get("hosts")
    if not isinstance(hosts, dict):
        return None
    found = hosts.get(host.lower())
    if not isinstance(found, dict):
        return None
    cert = found.get("cert_file")
    key = found.get("key_file")
    if not isinstance(cert, str) or not isinstance(key, str):
        return None
    if not Path(cert).is_file() or not Path(key).is_file():
        return None
    return cert, key


def read_tls_request(
    client: ssl.SSLSocket, deadline: Deadline, clock: Any
) -> tuple[str, str, dict[str, str]]:
    started = clock.monotonic()
    client.settimeout(min(deadline.idle_s, _remaining(deadline, clock, started)))
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        if clock.monotonic() - started >= deadline.total_s:
            raise _timeout("headers", "outcome_unknown", False, 0)
        try:
            chunk = client.recv(4096)
        except TimeoutError as exc:
            raise _timeout("headers", "outcome_unknown", False, 0) from exc
        if not chunk:
            raise _timeout("headers", "outcome_unknown", False, len(buffer))
        buffer += chunk
        if len(buffer) > 65536:
            raise FactoryError("TRANSPORT_CONFIG_INVALID", "内层请求过大", field="headers")
    head, _rest = buffer.split(b"\r\n\r\n", 1)
    lines = head.decode("iso-8859-1", errors="replace").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2:
        raise FactoryError("TRANSPORT_CONFIG_INVALID", "内层请求不合法", field="request")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return parts[0].upper(), parts[1], headers


def _deadline_section(value: Any, default: Mapping[str, Any], field: str) -> Deadline:
    data = dict(default)
    if value is None:
        return Deadline(float(data["connect_s"]), float(data["idle_s"]), float(data["total_s"]))
    if not isinstance(value, dict):
        raise FactoryError("TRANSPORT_CONFIG_INVALID", "时限必须是对象", field=field)
    unknown = set(value) - {"connect_s", "idle_s", "total_s"}
    if unknown:
        raise FactoryError("TRANSPORT_CONFIG_INVALID", "时限有未知字段", field=sorted(unknown)[0])
    data.update(value)
    numbers = []
    for key in ("connect_s", "idle_s", "total_s"):
        number = data[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or float(number) <= 0:
            raise FactoryError("TRANSPORT_CONFIG_INVALID", "时限必须是有限正数", field=key)
        numbers.append(float(number))
    connect_s, idle_s, total_s = numbers
    if connect_s > total_s or idle_s > total_s:
        raise FactoryError("TRANSPORT_CONFIG_INVALID", "connect/idle 不能大于 total", field=field)
    return Deadline(connect_s, idle_s, total_s)


def _inspection(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise FactoryError("TRANSPORT_CONFIG_INVALID", "tls_inspection 不合法", field="tls_inspection")
    unknown = set(value) - {"ca_file", "hosts"}
    if unknown or "ca_file" not in value or "hosts" not in value:
        raise FactoryError("TRANSPORT_CONFIG_INVALID", "tls_inspection 不合法", field="tls_inspection")
    if not isinstance(value["ca_file"], str) or not Path(value["ca_file"]).is_absolute():
        raise FactoryError("TRANSPORT_CONFIG_INVALID", "ca_file 必须是绝对路径", field="ca_file")
    hosts = value["hosts"]
    if not isinstance(hosts, dict) or not hosts:
        raise FactoryError("TRANSPORT_CONFIG_INVALID", "hosts 不合法", field="hosts")
    cleaned: dict[str, dict[str, str]] = {}
    for host, item in hosts.items():
        if not isinstance(host, str) or not isinstance(item, dict):
            raise FactoryError("TRANSPORT_CONFIG_INVALID", "hosts 不合法", field="hosts")
        cert = item.get("cert_file")
        key = item.get("key_file")
        if (
            not isinstance(cert, str)
            or not isinstance(key, str)
            or not Path(cert).is_absolute()
            or not Path(key).is_absolute()
        ):
            raise FactoryError("TRANSPORT_CONFIG_INVALID", "证书路径必须是绝对路径", field="cert_file")
        cleaned[host.lower()] = {"cert_file": cert, "key_file": key}
    return {"ca_file": value["ca_file"], "hosts": cleaned}


def _origin(scheme: str, host: str, port: int | None) -> str:
    default = 443 if scheme == "https" else 80
    shown = "" if port in (None, default) else f":{port}"
    return f"{scheme}://{host.lower()}{shown}"


def _canonical_origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise FactoryError("SERVICE_LOCATION_DENIED", "允许来源不合法", field="allowed_origins")
    return _origin(parsed.scheme, parsed.hostname, parsed.port)


def _remaining(deadline: Deadline, clock: Any, started: float) -> float:
    left = deadline.total_s - (clock.monotonic() - started)
    return max(0.001, left)


def _timeout(phase: str, outcome: str, retryable: bool, bytes_sent: int) -> FactoryError:
    return FactoryError(
        "TRANSPORT_TIMEOUT",
        "传输时限已到",
        phase=phase,
        outcome=outcome,
        retryable=retryable,
        bytes_sent=bytes_sent,
    )


def _http_bytes(
    method: str, path: str, host: str, port: int, headers: Mapping[str, str], data: bytes
) -> bytes:
    lines = [
        f"{method} {path} HTTP/1.1",
        f"Host: {host}:{port}",
        f"Content-Length: {len(data)}",
        "Connection: close",
    ]
    for key, value in headers.items():
        if key.lower() in {"host", "content-length", "connection"}:
            continue
        lines.append(f"{key}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + data


def _read_http(
    sock: socket.socket, deadline: Deadline, clock: Any, started: float, bytes_sent: int
) -> tuple[int, bytes, str]:
    buffer = b""
    last = clock.monotonic()
    while b"\r\n\r\n" not in buffer:
        _check_limits(deadline, clock, started, last, bytes_sent, "headers")
        sock.settimeout(min(deadline.idle_s, _remaining(deadline, clock, started)))
        try:
            chunk = sock.recv(4096)
        except TimeoutError as exc:
            raise _timeout("headers", "outcome_unknown", False, bytes_sent) from exc
        if not chunk:
            raise _timeout("headers", "outcome_unknown", False, bytes_sent)
        buffer += chunk
        last = clock.monotonic()
    head, rest = buffer.split(b"\r\n\r\n", 1)
    lines = head.decode("iso-8859-1", errors="replace").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    content_type = headers.get("content-type", "application/json")
    if "content-length" in headers:
        needed = int(headers["content-length"])
        while len(rest) < needed:
            _check_limits(deadline, clock, started, last, bytes_sent, "body")
            sock.settimeout(min(deadline.idle_s, _remaining(deadline, clock, started)))
            try:
                chunk = sock.recv(65536)
            except TimeoutError as exc:
                raise _timeout("body", "outcome_unknown", False, bytes_sent) from exc
            if not chunk:
                raise _timeout("body", "outcome_unknown", False, bytes_sent)
            rest += chunk
            last = clock.monotonic()
        return status, rest[:needed], content_type
    while True:
        _check_limits(deadline, clock, started, last, bytes_sent, "body")
        sock.settimeout(min(deadline.idle_s, _remaining(deadline, clock, started)))
        try:
            chunk = sock.recv(65536)
        except TimeoutError as exc:
            raise _timeout("body", "outcome_unknown", False, bytes_sent) from exc
        if not chunk:
            if "chunked" in headers.get("transfer-encoding", "").lower():
                return status, _decode_chunked(rest), content_type
            return status, rest, content_type
        rest += chunk
        last = clock.monotonic()


def _decode_chunked(data: bytes) -> bytes:
    """去掉 Transfer-Encoding: chunked 的长度帧，交给用量解析看正文。"""
    out = bytearray()
    index = 0
    while index < len(data):
        line_end = data.find(b"\r\n", index)
        if line_end < 0:
            break
        size_text = data[index:line_end].split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError:
            break
        start = line_end + 2
        end = start + size
        if end > len(data):
            out.extend(data[start:])
            break
        out.extend(data[start:end])
        index = end + 2 if data[end : end + 2] == b"\r\n" else end
        if size == 0:
            break
    return bytes(out)


def _check_limits(
    deadline: Deadline, clock: Any, started: float, last: float, bytes_sent: int, phase: str
) -> None:
    now = clock.monotonic()
    if now - started >= deadline.total_s or now - last >= deadline.idle_s:
        raise _timeout(phase, "outcome_unknown", False, bytes_sent)


def deadline_at(clock: Any, total_s: float) -> str:
    return utc_iso(clock.now() + timedelta(seconds=total_s))
