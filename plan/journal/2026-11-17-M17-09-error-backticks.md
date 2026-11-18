# M17-09 受阻：新错误码无法同时满足登记和旧文档测试

- 时间：2026-11-17
- 任务：M17-09 有界传输、CONNECT 与未知请求恢复
- 现象：`uv run python tools/harness.py accept M17-09` 退出码 1。contract 6 项已通过（failed=errors=skipped=0，total=6）。回归失败：`tests/acceptance/m1/test_m1_01_foundation.py::test_codes_documented_in_specs`，断言 `` `SERVICE_LOCATION_DENIED` `` 不在 spec/system 文本中。doccheck 同样报告 `SERVICE_LOCATION_DENIED`、`TRANSPORT_CANCELLED`、`TRANSPORT_CONFIG_INVALID`、`TRANSPORT_TIMEOUT` 已登记但没有任何 spec/system 文档以反引号提到它们。
- 规范依据：spec/system/operability.md 第 6 节，原文：“新增错误在对应任务登记 CODES”。同节表格列出 `TRANSPORT_CONFIG_INVALID`、`TRANSPORT_TIMEOUT`、`TRANSPORT_CANCELLED`、`SERVICE_LOCATION_DENIED`，单元格里没有反引号。tests/acceptance/m1/test_m1_01_foundation.py 要求每个 CODES 键以 `` `CODE` `` 出现在 spec/system。
- 我的理解：A. 按 operability 第 6 节把这四个码登记进 `mao.errors.CODES`，传输失败才能抛已登记的 FactoryError。B. 不登记，则 `FactoryError` 构造时直接 AssertionError，M17-09 的超时和地址拒绝无法按冻结错误码返回。两种做法都不能同时通过 M1 文档测试和 operability 错误表，因为表没有反引号，而 spec 受保护，不能为了旧测试去改它。
- 已经尝试：实现 `mao.transport`、迁移 `046_operability.sql`，并把模型代理和 CONNECT 接到有界时限。`tests/system/test_m17_09_transport.py` 六项通过。登记四个码后，accept 的 contract 通过，回归和 doccheck 因反引号缺失失败。
- 建议：在 operability.md 第 6 节错误表的码名上补反引号，只同步该文件哈希，然后 `task start M17-09` 再验收。不要删掉 CODES 登记，也不要改 M1 测试。

## 后续

- 2026-11-17：已按授权只给第 6 节十四个新错误码补反引号，并只更新 `spec/system/operability.md` 的锁哈希。`lock --verify` 通过后重新 `task start M17-09`。
