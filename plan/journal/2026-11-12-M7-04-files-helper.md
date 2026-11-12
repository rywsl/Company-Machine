# M7-04 受阻：验收辅助把秘密清单也排除了

- 时间：2026-11-12
- 任务：M7-04 备份、校验与恢复
- 现象：`tests/acceptance/m7/test_m7_04_backup.py` 的 `test_create_layout` 失败。`_files` 排除所有名为 `manifest.json` 的文件，因此右边没有 `config/secrets/manifest.json`。清单按 `backup.md` 4.2 列入了该文件，左边多这一项。同一测试又要求 `"config/secrets/manifest.json" in listed`。两条断言不能同时成立。其余 5 个备份测试通过；`test_cli_roundtrip` 在修好位置参数名后可通过。
- 规范依据：`spec/system/backup.md` 4.1 与 4.2，原文：“`config/secrets/manifest.json` 只有名字与分类，存在才有”；“`files`：除 `manifest.json` 外的每个文件”。
- 我的理解：A. 备份清单包含 `config/secrets/manifest.json`，符合规范，但过不了 `_files`。B. 不列入该文件，则过不了同一测试后面的 `expected in listed`。
- 已经尝试：实现 `create_backup` / `verify_backup` / `restore_backup`。钩子拒绝编辑该验收文件。
- 建议：把 `_files` 改成只排除备份根上的 `manifest.json`，不要排除 `config/secrets/manifest.json`。然后 `lock --update`。
