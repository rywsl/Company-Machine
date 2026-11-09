# M1 夹具 Registry

M1 验收测试与端到端测试使用的最小 Registry（说明见 `spec/system/testing.md` 第 6 节）。**受保护，只能由人修改。**

- 镜像引用里的 digest 是占位值（镜像名的 sha256）。Docker 测试由 `tests/fakes/fakelib.py` 的
  `render_registry(..., images=...)` 把它们替换成本机测试镜像的 image ID。
- `constitution/invariants.mrl` 必须与 `spec/mrl/constitution/invariants.mrl` 逐字节相同。
- 修改后必须保证 `uv run mrl registry check tests/fixtures/registry-m1` 没有任何诊断。
