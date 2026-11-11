# M2 夹具 Registry

M2–M7 验收测试使用的 Registry（说明见 `spec/system/testing.md` 第 4.3 节与各规范第 7 章）。**受保护，只能由人修改。**

- 在 M1 夹具的基础上增加产线、Source / Sink、转换、黄金样例（`goldens/`）与指标基线（`baselines/`）。
- 镜像引用里的 digest 是占位值。Docker 测试由 `tests/fakes/fakelib.py` 的 `render_registry(..., images=...)` 替换成本机测试镜像的 image ID。
- `constitution/invariants.mrl` 必须与 `spec/mrl/constitution/invariants.mrl` 逐字节相同。
- 修改后必须保证 `uv run mrl registry check tests/fixtures/registry-m2` 没有任何诊断。
