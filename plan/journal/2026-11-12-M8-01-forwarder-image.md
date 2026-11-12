# M8-01 受阻：Docker Desktop 转发镜像与受保护的 M6 实况夹具矛盾

- 时间：2026-11-12
- 任务：M8-01 M5–M7 审查纠偏
- 现象：本机 `docker info` 的 OperatingSystem 是 `Docker Desktop (containerized)`。`tests/acceptance/m6/live_world.py` 写出的 `factory.json` 没有 `serve.forwarder_image`，随后 `daemon.start()` 会调用 `start_owned_proxies`。用同样的缺口复现：

```text
uv run python probe_fwd.py
CODE EGRESS_PROXY_UNAVAILABLE
DETAIL {'reason': 'forwarder-image'}
MESSAGE 没有配置转发镜像
```

  `tests/acceptance/m8/test_m8_01_hardening.py` 8 个已通过。`tests/system/test_hardening.py` 8 个已通过。阶段门 M8 继承 M7，M7 含 `acceptance-m6` 且 `MAO_REQUIRE_DOCKER=1`。实况夹具会在启动代理时得到上述错误，阶段门不能通过。
- 规范依据：`spec/system/hardening.md` 5.4 第 2 条。原文：“镜像取 `serve.forwarder_image`（4.2）。没有配置 → `EGRESS_PROXY_UNAVAILABLE`，`detail.reason = "forwarder-image"`。**禁止**写死镜像名。”
- 我的理解：A. 按 5.4 实现：没有配置就拒绝启动转发容器。M6 实况测试在 Docker Desktop 上启动守护进程，工厂配置里没有这个字段，测试失败。B. 缺配置时回退到写死的 `node:22-bookworm-slim` 或 `tools/live_versions.json`。实况测试可能继续通过，但违反 5.4 和“不要写死镜像”。
- 已经尝试：转发容器已按 5.4 实现（镜像只从 `serve.forwarder_image` 读取，容器名、收紧参数、失败删除、关闭删除）。没有改受保护的 `live_world.py`，没有写死镜像。没有跑阶段门，避免用一次已知的矛盾失败代替阻塞。
- 建议：由人在 `tests/acceptance/m6/live_world.py` 的 `serve` 段写入带 `@sha256:` 的 `forwarder_image`（可与 `tools/live_versions.json` 的 `base` 相同），再重新登记受保护文件。实现侧不用改。
