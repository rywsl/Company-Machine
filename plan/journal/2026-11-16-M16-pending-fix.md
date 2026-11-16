# M16 更正：失败轮取消 pending，挂载路径保持到安装

- 时间：2026-11-16
- 原因：复核指出失败轮仍会在下一成功轮安装接管，pending 会立刻把已挂载的接管树改回默认路径，工作轮提示和冷检查点仍读 `agents/<name>/workspace`。

## 补上的行为

- 工作轮不是 ok，以及 `recover` 把进行中的工作轮标成 interrupted 时，取消 phase=pending，保留当前挂载。之后的成功轮不再安装这次接管。
- pending 只记录试验，不改 `current_path` / `previous_path`。已安装的接管树继续挂载，直到真正安装新工作区。pending 的 abort 也不把路径退回更早的目录。
- 工作轮提示用 `workspace_path()` 判断 `HANDOFF.md`，与沙箱挂载的是同一棵树。
- 冷检查点的分类、复制和恢复都走已解析工作区。只存在于接管树里的已声明业务文件进入 `DEST/business`，恢复时写回该挂载路径。

## 验证

`tests/system/test_assets_mount.py` 9 项通过，覆盖失败退出、recover、第二次 pending、挂载树提示和检查点复制。同次重跑 M16-03、M16-04、M16-06、M13-04 与既有交接/检查点系统测试，29 passed。
