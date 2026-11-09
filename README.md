# Codex Project Recovery

恢复 Windows ChatGPT/Codex 桌面应用在切换账号后丢失的**本地项目清单与聊天分组**。

适用场景：侧栏显示“没有项目”，聊天仍在“最近”列表，本地项目目录仍存在，而且原清单保留在另一份本机账号状态中。这个工具把经过校验的清单复制到**当前登录账号的空项目配置**中，保留其他账号的原始记录。

这是针对应用内部数据格式的非官方恢复工具，不是 OpenAI 支持的迁移接口。它不能修复所有“项目消失”问题，也不保证未来版本继续使用相同格式。原型在 Windows 桌面版 `26.1103.32239` / 包版本 `26.1103.4239.0` 上完成过恢复；此仓库的通用版本包含合成数据测试，未对所有应用版本做兼容验证。

## 快速使用

需要 Windows、Python 3.10 或更高版本，无第三方依赖。

1. 在桌面应用中登录**希望恢复到的账号**。
2. 下载或克隆本仓库，双击 `restore.cmd`，或在终端运行：

   ```powershell
   python -X utf8 codex_project_recovery.py interactive
   ```

3. 根据打印的项目数量、聊天关联数量选择源账号编号。编号只标识当前本机快照，不显示账号邮箱；有多个源且无法判断时，先停止，不要猜。
4. 检查恢复数量，输入 `y` 后，保留工具的终端窗口，在 30 分钟内通过桌面应用的 **文件 → 退出 ChatGPT** 完全退出。关闭主窗口可能只会最小化到托盘。其他 Codex CLI 进程也需要结束。
5. 工具检测应用退出后，再次校验、备份并写入。显示 `"stage": "restored"` 后重新打开应用，核对项目与聊天分组。

恢复只是复制当前清单。以后各账号新增的项目不会自动互相同步；这个工具不驻留、不监控，也不会自动切换账号。

## 分步使用

只读检查，不修改应用数据：

```powershell
python -X utf8 codex_project_recovery.py diagnose
```

选择 `diagnose` 显示的源编号，生成恢复计划。此时不会写入 Codex 配置：

```powershell
python -X utf8 codex_project_recovery.py prepare --source 1
```

复制输出中的真实计划路径。下面的 `<run-directory>` 需要替换：

```powershell
python -X utf8 codex_project_recovery.py apply --plan ".recovery\<run-directory>\restore-plan.json" --wait-seconds 1800
```

`apply` 不带 `--wait-seconds` 时，检测到应用运行会直接停止。要在恢复后自动打开应用，可以额外提供 `--reopen "完整的 ChatGPT.exe 路径"`；不提供时由用户手动打开。工具不会关闭或强制终止应用。

默认读取 `CODEX_HOME`，未设置则使用当前 Windows 用户的 `.codex` 文件夹。可显式指定目录，全局参数放在子命令之前：

```powershell
python -X utf8 codex_project_recovery.py --codex-home "D:\CodexData" --work-dir "D:\RecoveryFiles" diagnose
```

## 校验与写入范围

- 使用现存的项目 ID、目录和显式聊天归属，不根据聊天标题或工作目录猜测归属。
- 对照只读的 `state_5.sqlite` 校验本地项目、目录和聊天 ID。找不到的本地聊天关联会跳过，原因写入本地计划。
- 可复制已保存的远程项目登记与远程聊天归属，但不连接远程主机，也不验证远程聊天是否仍存在。
- 当前账号已有项目或归属、源数据变化、账号切换、目录缺失、项目映射冲突，或发现云项目绑定字段时，停止操作。
- 写入前保存全局状态原件和一致的 SQLite 备份，并检查数据库完整性；备份失败时不继续写入。
- 仅补充项目清单、项目顺序、外观和聊天分组等相关字段，原账号快照与无关设置保留。
- 不修改实时 SQLite 数据库、登录凭据、`config.toml`、`session_index.jsonl`、聊天正文或项目文件；不使用网络。
- 写入前再次检查应用已退出、状态文件未变化。这个检查无法替代操作系统级独占锁：恢复期间请勿手动重新启动应用。

源状态必须包含可与本机数据库对应的项目映射。当前版本只支持“恢复到空清单”，不自动合并已有项目，也不用于获取其他账号的云项目权限。

## 备份和隐私

默认工作目录为仓库内的 `.recovery/`。每次生成计划使用独立目录，应用时在其下创建 `backup-*` 子目录，保存：

- `.codex-global-state.json` 原件及原有 `.bak`；
- `state_5.sqlite` 的一致性备份；
- 恢复计划和结果状态。

**计划和备份含本机路径、项目名称、内部账号标识及其他本地状态，不要上传。** `.gitignore` 已排除这些文件。本仓库只包含工具源码、文档和合成测试。

如需撤销，先完全退出应用，再检查本次备份。恢复备份的全局状态 JSON 会回退这次恢复之后的相关界面配置，不能在已有新改动时盲目覆盖；聊天数据库没有被工具改写，无须覆盖它。请保留备份，直到项目和对话分组核验完毕。

## 测试

```powershell
python -X utf8 -m unittest discover -s tests -v
```

测试仅使用仓库内 `.test-data/` 中的临时合成资料，覆盖账号变更、运行中应用、状态竞争、项目映射、缺失聊天、备份和原状态保留等关键情形，不接触用户真实 Codex 配置。

## 已知社区问题

相关报告可参考 [openai/codex #59895](https://github.com/openai/codex/issues/59895) 和 [#58754](https://github.com/openai/codex/issues/58754)。报告中的现象可能有不同根因，本工具只处理上述特定的本地账号状态恢复场景。
