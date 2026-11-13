"""命令行共用：打开工厂、选快照、JSON 与退出码。"""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import os
import sys
import threading
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from mao.base import canonical_json, utc_iso
from mao.context import Context
from mao.errors import FactoryError
from mao.sandbox import DockerProvider, LocalProvider

GLOBAL = argparse.ArgumentParser(add_help=False)
GLOBAL.add_argument("--root", default=argparse.SUPPRESS)
GLOBAL.add_argument("--registry", default=argparse.SUPPRESS)
GLOBAL.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
GLOBAL.add_argument("--provider", choices=("docker", "local"), default=argparse.SUPPRESS)
GLOBAL.add_argument("--local-commands", default=argparse.SUPPRESS)
GLOBAL.add_argument("--epoch", default=argparse.SUPPRESS)

GLOBAL_DEFAULTS = {
    "root": None,
    "registry": None,
    "json": False,
    "provider": "docker",
    "local_commands": None,
    "epoch": None,
}

_FACILITY = {
    "DOCKER_UNAVAILABLE",
    "DOCKER_FAILED",
    "IMAGE_NOT_FOUND",
    "GIT_FAILED",
    "REGISTRY_NOT_CONFIGURED",
    "BACKEND_UNAVAILABLE",
    "MODEL_PROXY_BIND_FAILED",
    "EGRESS_PROXY_UNAVAILABLE",
    "RESTORE_FAILED",
    "CONSOLE_BIND_FAILED",
    "GC_NOT_CONFIGURED",
}


def human_by() -> str:
    return "human:" + getpass.getuser()


def registry_path(args: argparse.Namespace) -> Path:
    return Path(args.registry or os.environ.get("FACTORY_REGISTRY") or "registry")


def governance_registry(ctx: Context) -> str | None:
    path = ctx.root / "factory.json"
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    governance = body.get("governance") if isinstance(body, dict) else None
    if isinstance(governance, dict) and governance.get("registry"):
        return str(governance["registry"])
    return None


def open_context(args: argparse.Namespace) -> Context:
    root = Path(args.root or os.environ.get("FACTORY_ROOT") or ".factory")
    if args.provider == "local":
        commands = load_commands(args.local_commands) if args.local_commands else {}
        provider: Any = LocalProvider(root, commands=commands)
    else:
        provider = DockerProvider(root)
    from mao.backends import open_context as open_backend

    ctx = open_backend(root, provider=provider, backend=root / "backend-active.json")
    from mao.egress import discover_proxies

    ctx.proxies = discover_proxies(ctx)
    if ctx.proxies is not None:
        provider.proxies = ctx.proxies
        if ctx.proxies.alias:
            provider.egress_alias = ctx.proxies.alias
    return ctx


def install_stop_signals(stop: threading.Event) -> None:
    import signal

    def _stop(signum: int, frame: object) -> None:
        del signum, frame
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _stop)


def open_snapshot(
    ctx: Context, args: argparse.Namespace, *, route_key: str | None = None, epoch: str | None = None
) -> Any:
    from mao.governance.epochs import EpochStore
    from mao.governance.rollout import EpochRouter
    from mao.snapshot import Snapshot

    if args.epoch:
        if governance_registry(ctx) is None:
            raise FactoryError(
                "REGISTRY_NOT_CONFIGURED", "没有配置 Registry", path=str(ctx.root / "factory.json")
            )
        return EpochStore(ctx).materialize(args.epoch)
    if governance_registry(ctx):
        store = EpochStore(ctx)
        if epoch is not None:
            return store.materialize(epoch)
        if route_key is not None:
            return store.materialize(EpochRouter(ctx, store).choose(route_key))
        return store.materialize(store.current())
    return Snapshot.load(registry_path(args))


def load_commands(path: str) -> dict[str, list[str]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FactoryError("CLI_USAGE", "local-commands 不是 JSON 对象", path=path) from exc
    if not isinstance(data, dict):
        raise FactoryError("CLI_USAGE", "local-commands 不是 JSON 对象", path=path)
    commands: dict[str, list[str]] = {}
    for key, value in data.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, list)
            or any(not isinstance(item, str) for item in value)
        ):
            raise FactoryError("CLI_USAGE", "local-commands 的值必须是字符串列表", path=path)
        commands[key] = list(value)
    return commands


def split_pair(text: str, message: str) -> tuple[str, str]:
    if "=" not in text:
        raise FactoryError("CLI_USAGE", message)
    key, value = text.split("=", 1)
    return key, value


def actor_from_env() -> str:
    actor = os.environ.get("FACTORY_ACTOR")
    if not actor:
        raise FactoryError("CLI_USAGE", "缺少 FACTORY_ACTOR")
    return actor


def to_jsonable(obj: Any) -> Any:
    if hasattr(obj, "to_json") and callable(obj.to_json):
        return to_jsonable(obj.to_json())
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {item.name: to_jsonable(getattr(obj, item.name)) for item in dataclasses.fields(obj)}
    if isinstance(obj, StrEnum):
        return obj.value
    if isinstance(obj, (tuple, list)):
        return [to_jsonable(item) for item in obj]
    if isinstance(obj, (set, frozenset)):
        return [to_jsonable(item) for item in sorted(obj, key=str)]
    if isinstance(obj, dict):
        if any(not isinstance(key, str) for key in obj):
            raise TypeError("dict 键必须是字符串")
        return {key: to_jsonable(value) for key, value in obj.items()}
    if isinstance(obj, Path):
        return obj.as_posix()
    if isinstance(obj, datetime):
        return utc_iso(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    raise TypeError(type(obj).__name__)


def emit(args: argparse.Namespace, obj: Any, text: str | None = None) -> None:
    if args.json:
        payload = {"ok": True} if obj is None else to_jsonable(obj)
        print(canonical_json(payload))
        return
    if text is not None:
        print(text)
        return
    if obj is None:
        print("ok")
        return
    print(canonical_json(to_jsonable(obj)))


def exit_code(exc: FactoryError) -> int:
    if exc.code == "CLI_USAGE":
        return 2
    if exc.code == "REGISTRY_INVALID":
        return 3
    if exc.code in _FACILITY:
        return 4
    return 1


def report_error(args: argparse.Namespace, exc: FactoryError) -> int:
    from mao.egress import SecretStore

    root = Path(args.root or os.environ.get("FACTORY_ROOT") or ".")
    store = SecretStore(root)
    body = store.redact_obj(exc.to_json())
    if getattr(args, "json", False):
        print(canonical_json({"error": body}))
    else:
        print(f"{exc.code}: {store.redact(exc.message)}", file=sys.stderr)
    return exit_code(exc)


def finish(
    args: argparse.Namespace, obj: Any, *, code: int = 0, text: str | None = None, null: bool = False
) -> int:
    if args.json and obj is None and null:
        print(canonical_json(None))
        return code
    emit(args, obj, text)
    return code
