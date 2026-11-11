# 参与共建

公开协作版 v0.2.0 · 2026-11-11。

欢迎使用合成内容试验、报告体验问题、审阅实现和提交聚焦的 Pull Request。先看[共建路线](docs/ROADMAP.md)，选择一个能独立复现和验收的问题。许可证尚未确定，贡献的许可安排需先与项目维护者确认。

## 报告一个问题

说明使用场景、系统与版本、当前行为、期望行为、复现步骤及最小验收。检索改进提供查询集、无答案样例和耗时，不只展示成功案例。客户端适配写清具体宿主、工具调用方式和真实会话验证范围。

仅使用虚构或充分脱敏的资料。不要上传真实数据库、来源原文、认证、聊天、请求日志、备份、客户资料或含这些内容的截图。疑似凭据不要贴到公开 Issue。

## 开发与验证

安装和合成演示见 [README](README.md)。当前依赖锁仍面向 Windows x64 / Python 3.13；不同平台的完整部署尚待验证。项目可位于任意符合[路径边界](docs/部署与运行目录.md)的普通目录，不要求固定盘符。

```powershell
$python = '.\.venv-0.3.0\Scripts\python.exe'
& $python -X utf8 -B -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_directory_rendering_051.cjs
node --check hub/workbench-v0.7.0/workbench.js
& $python -X utf8 -B scripts/check_release_contract.py
& $python -X utf8 -B scripts/check_public_repository.py
```

报告实际通过、失败、跳过和未运行。`test_codex_adapter_010.py` 属于可选外部适配检查，只有显式提供 `SMH_CODEX_ADAPTER` 才会载入该脚本；默认跳过，不自动寻找个人配置或创建适配器。其他历史集成脚本也不属于默认体验入口。

可选工具使用显式环境变量：`SMH_BROWSER_LAUNCHER` 指向你已核准的浏览器启动脚本；`SMH_CODEX_EXECUTABLE` 选择 Codex 可执行文件；`SMH_CODEX_HOME` 仅供你主动运行的历史回执检查使用。不要为了跑测试而复制他人的个人配置。没有外部工具时保留未验状态。

真实 CPU / CUDA、Qdrant、浏览器及原生客户端的验收单独记录。夹具、静态检查与版本契约不能代替这些验证。

## 提交改动

从 `main` 创建短期分支，一次 PR 处理一个问题。显式检查提交文件，不提交依赖、模型、缓存、记忆数据和认证。PR 包含触发场景、修改后行为、范围、实际验证及未验项。

- 改可见行为时，同步 `docs/使用与系统说明.md`。
- 改接入或字段时，同步客户端契约、指南及相关测试。
- 改 Schema 时提供显式迁移，不在启动时自动改库。
- 保留权限、修订、锁定、幂等和清除保护；不以降低校验换取通过。
- 路径与命令示例使用相对位置或配置项，不加入个人电脑的绝对目录、用户名或私人仓库依赖。

当前没有自动部署工作流。提交源码不应触发对任何使用者电脑或记忆数据的操作。
